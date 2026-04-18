#!/usr/bin/env python3
"""
Wake-word recognition lab.

Prints a candidate word, waits for you to say it, records via mic,
transcribes with the SAME faster-whisper small model production uses,
and scores whether the transcript contains the candidate. 5 trials
per word. Summary at the end ranks by hit rate.

Default: run all groups with 0.6s silence cutoff (matches production).

Targeted test of a single word:
    python voice/wake_word_lab.py --word friday --silence 0.3

Run:
    cd ~/nexus && source venv/bin/activate && python voice/wake_word_lab.py
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio import transcribe, record_speech, get_whisper

# Candidate pools. Edit freely; first word in each group is the
# current production trigger we're trying to beat.
CANDIDATES = {
    "Claude trigger (currently 'friday')": [
        "friday", "monday", "tuesday", "saturday",
        "coder", "pilot", "captain",
    ],
    "Claudia trigger (currently 'wednesday')": [
        "wednesday", "question", "ponder", "quickly",
        "curious", "thinker",
    ],
    "Exit to Gemini (currently 'jarvis')": [
        "jarvis", "gemini", "hello", "back", "over",
    ],
    "Cancel / stop (currently 'stop friday')": [
        "cancel", "abort", "scratch", "nevermind", "forget",
    ],
}

TRIALS = 5


def run_trial(candidate: str, silence_s: float) -> tuple[bool, str]:
    """Record until silence, return (hit, transcript)."""
    audio = record_speech(
        silence_duration=silence_s,
        max_duration=4.0,
        wait_timeout=6.0,
    )
    if audio is None:
        return False, "<no speech>"
    text = transcribe(audio).strip().lower()
    import re
    norm = re.sub(r"[^a-z ]+", " ", text)
    norm = " ".join(norm.split())
    hit = bool(re.search(rf"\b{re.escape(candidate.lower())}\b", norm))
    return hit, text


def run_one_word(word: str, silence_s: float, trials: int) -> list[tuple[bool, str]]:
    """Run `trials` trials for a single word at the given silence cutoff."""
    print(f"\n  Word: {word.upper()}  (silence_duration={silence_s}s)")
    input(f"  Press Enter to start {trials} trials, then say '{word}' each time...")
    results: list[tuple[bool, str]] = []
    for i in range(trials):
        print(f"    [{i + 1}/{trials}] Say: {word.upper()}  ", end="", flush=True)
        hit, text = run_trial(word, silence_s)
        mark = "✓" if hit else "✗"
        print(f" {mark}  (heard: {text!r})")
        results.append((hit, text))
        time.sleep(0.4)
    hits = sum(1 for h, _ in results if h)
    print(f"    → {hits}/{trials} hits ({100 * hits // trials}%)")
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--word", help="Test only this single word (skips all groups).")
    parser.add_argument("--silence", type=float, default=0.6,
                        help="silence_duration passed to record_speech (default 0.6)")
    parser.add_argument("--trials", type=int, default=TRIALS)
    args = parser.parse_args()

    print("Loading Whisper... (first time ~2s)")
    get_whisper()

    # ── Single-word targeted test ─────────────────────────────────────
    if args.word:
        trials = run_one_word(args.word, args.silence, args.trials)
        hits = sum(1 for h, _ in trials if h)
        bar = "█" * hits + "░" * (args.trials - hits)
        mishears = [t for h, t in trials if not h and t != "<no speech>"]
        extra = f"  misheard: {', '.join(set(mishears))[:80]}" if mishears else ""
        print(f"\n  {args.word:12s} {bar} {hits}/{args.trials}  "
              f"@ silence={args.silence}s{extra}")
        return

    # ── Full sweep across groups ──────────────────────────────────────
    all_results: dict[str, dict[str, list[tuple[bool, str]]]] = {}
    for group, words in CANDIDATES.items():
        print(f"\n{'=' * 70}")
        print(f"  {group}")
        print("=" * 70)
        group_results: dict[str, list[tuple[bool, str]]] = {}
        for word in words:
            group_results[word] = run_one_word(word, args.silence, args.trials)
        all_results[group] = group_results

    print(f"\n{'=' * 70}")
    print(f"  SUMMARY — silence_duration={args.silence}s")
    print("=" * 70)
    for group, group_results in all_results.items():
        print(f"\n{group}")
        ranked = sorted(
            group_results.items(),
            key=lambda kv: -sum(1 for h, _ in kv[1] if h),
        )
        for word, trials in ranked:
            hits = sum(1 for h, _ in trials if h)
            bar = "█" * hits + "░" * (args.trials - hits)
            mishears = [t for h, t in trials if not h and t != "<no speech>"]
            extra = f"  misheard: {', '.join(set(mishears))[:80]}" if mishears else ""
            print(f"  {word:12s} {bar} {hits}/{args.trials}{extra}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
