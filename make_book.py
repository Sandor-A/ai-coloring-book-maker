#!/usr/bin/env python3
"""
make_book.py: from a one-line request to a printable coloring book.

  python make_book.py "space themed ABC coloring book for my 2-year-old"

What happens:
  1. Plan    An LLM turns the request into a book plan (title, age group, one scene per page)
             and saves it as plan.json. You see the plan and confirm before any pictures are drawn.
  2. Draw    An image model draws every page. A recurring character is drawn first and then
             passed along as a reference image, so it looks the same throughout the book.
  3. Check   Each picture gets pixel checks and an AI review. Failed pictures are redrawn with
             the problems fed back into the prompt, up to --retries times.
  4. Build   The pictures are laid out on A4 pages with letters, captions and a cover, as a PDF.

Everything lands in books/<title>/: plan.json, prompts.txt, art/, report.txt and the PDF.
Runs are resumable: finished pictures are kept, so running again only draws what's missing.

Examples
  python make_book.py "dinosaur coloring book for a 5-year-old, 12 pages"
  python make_book.py "ocean animals ABC" --plan-only        # stop after planning, edit plan.json
  python make_book.py --resume books/ocean-abc                # continue an existing book
  python make_book.py --resume books/ocean-abc --redo q,k     # redraw particular pages
  python make_book.py "space ABC" --text mock --images mock   # free offline test run
"""

import argparse
import json
import re
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from layout import build_pdf
from planner import PlanError, make_plan, validate_plan
from prompts import AGE_GROUPS, build_prompt
from providers import make_image_provider, make_text_provider, png_bytes
from qa import pixel_issues, vision_issues

print_lock = threading.Lock()


def log(message):
    with print_lock:
        print(message, flush=True)


# ------------------------------------------------------------------ setup

def parse_args():
    p = argparse.ArgumentParser(description="Make a coloring book PDF, for any age, from a one-line request.",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Examples" + __doc__.split("Examples", 1)[1])
    p.add_argument("request", nargs="?", help='what the book should be, e.g. "farm animals for a 3-year-old"')
    p.add_argument("--resume", metavar="FOLDER", help="continue a book folder made earlier")
    p.add_argument("--redo", metavar="KEYS", help="comma-separated pages to redraw, e.g. q,k,cover")
    p.add_argument("--plan-only", action="store_true", help="stop after planning so you can edit plan.json")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation before drawing")

    p.add_argument("--age", choices=sorted(AGE_GROUPS), help="age group (otherwise taken from the request)")
    p.add_argument("--pages", type=int, help="number of pages for a themed book (ABC books always have 26)")

    p.add_argument("--text", choices=["anthropic", "gemini", "mock"], help="planning and review model provider")
    p.add_argument("--text-model", help="override the planning/review model name")
    p.add_argument("--images", choices=["openai", "gemini", "mock"], help="image provider")
    p.add_argument("--image-model", help="override the image model name")
    p.add_argument("--quality", choices=["low", "medium", "high"], default="high",
                   help="OpenAI image quality (default: high)")
    p.add_argument("--no-review", action="store_true", help="skip the AI review (pixel checks still run)")
    p.add_argument("--retries", type=int, default=2, help="redraws allowed per failed picture (default: 2)")
    p.add_argument("--workers", type=int, default=3, help="pictures drawn at the same time (default: 3)")

    p.add_argument("--font", help="bold rounded .ttf font, e.g. Fredoka-Bold.ttf")
    p.add_argument("--blank-backs", action="store_true", help="blank page behind each page, for double-sided printing")
    p.add_argument("--no-cover", action="store_true", help="leave out the cover")
    p.add_argument("--out-dir", default="books", help="where book folders are created (default: books)")
    args = p.parse_args()
    if not args.request and not args.resume:
        p.error('give a request, e.g. make_book.py "space ABC for a 2-year-old", or --resume FOLDER')
    return args


def load_env_file():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def new_book_dir(out_dir, title):
    slug = re.sub(r"[^a-z0-9]+", "-", title.replace("|", " ").lower()).strip("-")[:40] or "book"
    path, n = Path(out_dir) / slug, 2
    while path.exists():
        path, n = Path(out_dir) / f"{slug}-{n}", n + 1
    path.mkdir(parents=True)
    return path


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------- jobs

def make_jobs(plan, cover=True):
    """One job per picture: the cover (optional) plus every page."""
    jobs = []
    if cover:
        jobs.append({"key": "cover", "label": "cover", "scene": plan["cover_scene"],
                     "features_character": plan["character"] is not None})
    for page in plan["pages"]:
        label = f"{page['letter']} {page['caption']}" if page["letter"] else f"{page['key']} {page['caption']}"
        jobs.append({"key": page["key"], "label": label, "scene": page["scene"],
                     "features_character": page["features_character"]})
    return jobs


def pick_anchor(jobs):
    """The page that defines the character's look: the first page that shows them (the cover
    comes last, because it's a busier picture)."""
    showing = [j for j in jobs if j["features_character"]]
    pages_first = [j for j in showing if j["key"] != "cover"] + [j for j in showing if j["key"] == "cover"]
    return pages_first[0] if pages_first else None


def write_prompts_file(book_dir, plan, jobs, anchor):
    lines = [f"Prompts for {plan['title'].replace('|', ' ')}",
             "Each prompt makes one square picture. Pages marked [reference] should get the",
             "character's picture attached as a reference image.", ""]
    for job in jobs:
        uses_ref = anchor is not None and job["features_character"] and job["key"] != anchor["key"]
        lines.append(f"=== {job['label']}  ->  art/{job['key']}.png" + ("  [reference]" if uses_ref else ""))
        lines.append(build_prompt(job["scene"], plan["age"],
                                  plan["character"] if job["features_character"] else None, uses_ref))
        lines.append("")
    (book_dir / "prompts.txt").write_text("\n".join(lines), encoding="utf-8")


# -------------------------------------------------------------- drawing

def draw_job(job, plan, art_dir, images, reviewer, retries, reference, status):
    """Draw one picture, check it, and redraw it with feedback until it passes or retries run out.
    The attempt with the fewest problems becomes art/<key>.png."""
    key, label, age = job["key"], job["label"], plan["age"]
    character = plan["character"] if job["features_character"] else None
    reference = reference if job["features_character"] else None
    attempts_dir = art_dir / "attempts"
    attempts_dir.mkdir(parents=True, exist_ok=True)

    fixes, best, error, attempt = None, None, None, 0
    while attempt < retries + 1:
        attempt += 1
        prompt = build_prompt(job["scene"], age, character, reference is not None, fixes)
        try:
            data = png_bytes(images.generate(prompt, reference))
        except Exception as err:
            if reference is not None:
                log(f"  [{label}] drawing with the reference image failed; trying without it")
                reference, attempt = None, attempt - 1
                continue
            error = f"{type(err).__name__}: {err}"
            log(f"  [{label}] drawing failed: {error}")
            break

        attempt_path = attempts_dir / f"{key}_{attempt}.png"
        attempt_path.write_bytes(data)
        issues = pixel_issues(data, age)
        if not issues and reviewer is not None:
            try:
                issues = vision_issues(reviewer, data, job["scene"], age, character)
            except Exception as err:
                log(f"  [{label}] review failed ({type(err).__name__}); keeping the picture unreviewed")

        if best is None or len(issues) < len(best[0]):
            best = (issues, attempt_path)
        if not issues:
            log(f"  [{label}] done (attempt {attempt})")
            break
        log(f"  [{label}] attempt {attempt} needs fixing: " + "; ".join(issues))
        fixes = issues

    if best:
        shutil.copyfile(best[1], art_dir / f"{key}.png")
    with print_lock:
        status[key] = {"attempts": attempt, "passed": bool(best) and not best[0],
                       "issues": best[0] if best else [], "error": error}


def draw_all(todo, anchor, plan, book_dir, images, reviewer, retries, workers, status):
    art_dir = book_dir / "art"
    art_dir.mkdir(exist_ok=True)
    reference = None
    if anchor is not None:
        if anchor in todo:
            log(f"Drawing the character first ({anchor['label']}), to use as a reference for the other pages")
            draw_job(anchor, plan, art_dir, images, reviewer, retries, None, status)
            todo = [j for j in todo if j is not anchor]
        anchor_art = art_dir / f"{anchor['key']}.png"
        reference = anchor_art if anchor_art.is_file() else None

    if todo:
        log(f"Drawing {len(todo)} pictures, {workers} at a time")
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for future in [pool.submit(draw_job, job, plan, art_dir, images, reviewer, retries, reference, status)
                           for job in todo]:
                future.result()


# ------------------------------------------------------------------- main

def main():
    args = parse_args()
    load_env_file()
    text_provider = None

    # 1. Plan
    if args.resume:
        book_dir = Path(args.resume)
        raw_plan = load_json(book_dir / "plan.json", None)
        if raw_plan is None:
            sys.exit(f"No plan.json in {book_dir}")
    else:
        text_name, text_provider = make_text_provider(args.text, args.text_model)
        print(f"Planning the book with {text_name}...")
        try:
            raw_plan, _ = make_plan(text_provider, args.request, args.age, args.pages)
        except PlanError as err:
            sys.exit(str(err))
        book_dir = new_book_dir(args.out_dir, raw_plan["title"])
    try:
        plan, warnings = validate_plan(raw_plan)
    except PlanError as err:
        sys.exit(f"plan.json has a problem: {err}")
    save_json(book_dir / "plan.json", plan)

    jobs = make_jobs(plan, cover=not args.no_cover)
    anchor = pick_anchor(jobs)
    write_prompts_file(book_dir, plan, jobs, anchor)

    print(f"\n{plan['title'].replace('|', ' ')}  ({len(plan['pages'])} pages, {AGE_GROUPS[plan['age']]['label']})")
    print("  " + ", ".join(p["caption"] for p in plan["pages"]))
    if plan["character"]:
        print(f"  Character: {plan['character']}")
    for w in warnings:
        print(f"  Note: {w}")
    print(f"  Folder: {book_dir}")

    if args.plan_only:
        print(f"\nPlan saved. Edit {book_dir / 'plan.json'} if you like, then run:\n"
              f"  python make_book.py --resume {book_dir}")
        return

    # 2 + 3. Draw and check
    art_dir = book_dir / "art"
    redo = {k.strip().lower() for k in (args.redo or "").split(",") if k.strip()}
    unknown = redo - {j["key"] for j in jobs}
    if unknown:
        sys.exit(f"--redo: no such page(s): {', '.join(sorted(unknown))}. "
                 f"Pages are: {', '.join(j['key'] for j in jobs)}")
    todo = [j for j in jobs if j["key"] in redo or not (art_dir / f"{j['key']}.png").is_file()]

    status = load_json(book_dir / "status.json", {})
    if todo:
        image_name, images = make_image_provider(args.images, args.image_model, args.quality)
        reviewer = None
        if not args.no_review:
            if text_provider is None:
                try:
                    _, text_provider = make_text_provider(args.text, args.text_model)
                except SystemExit:
                    print("  No planning/review API key found, so pictures get pixel checks only.")
            reviewer = text_provider

        print(f"\nAbout to draw {len(todo)} pictures with {image_name}"
              f"{' and review each one' if reviewer else ''}. Pictures that fail the checks are "
              f"redrawn up to {args.retries} more times each.")
        if not args.yes:
            if not sys.stdin.isatty():
                sys.exit("Add --yes to run without confirmation.")
            if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
                print(f"Stopped. Pick up later with: python make_book.py --resume {book_dir}")
                return
        try:
            draw_all(todo, anchor, plan, book_dir, images, reviewer, args.retries, args.workers, status)
        finally:
            save_json(book_dir / "status.json", status)
    else:
        print("\nAll pictures already exist; building the PDF.")

    # 4. Build
    pdf_path = book_dir / f"{book_dir.name}.pdf"
    missing, failed = build_pdf(plan, art_dir, pdf_path, args.font, args.blank_backs, cover=not args.no_cover)

    labels = {j["key"]: j["label"] for j in jobs}
    report = [f"{plan['title'].replace('|', ' ')}: {pdf_path}", ""]
    flagged = {k: s for k, s in status.items() if k in labels and (not s.get("passed") or s.get("error"))}
    if missing:
        report.append("No picture yet (left blank in the PDF): " + ", ".join(labels[k] for k in missing))
    if failed:
        report.append("Had a picture but it couldn't be placed (page kept its letter/caption; redraw with --redo):")
        for key, err in failed:
            report.append(f"  {labels.get(key, key)}: {err}")
    if flagged:
        report.append("Kept, but still had problems after all retries (redraw with --redo):")
        for key, s in flagged.items():
            report.append(f"  {labels[key]}: " + ("; ".join(s["issues"]) or s.get("error") or "unknown"))
    if not missing and not failed and not flagged:
        report.append("Every picture passed its checks.")
    (book_dir / "report.txt").write_text("\n".join(report), encoding="utf-8")
    print("\n" + "\n".join(report))


if __name__ == "__main__":
    main()
