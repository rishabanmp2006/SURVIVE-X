#!/usr/bin/env python3
"""
SURVIVE-X — Qwen3.5-0.8B Minimal Offline Interactive Chatbot
Hardware Target: Arduino UNO Q (2GB RAM, Quad Cortex-A53)
Host: macOS Darwin arm64 (CPU-only, 4 threads, -ngl 0)

Memory Architecture:
- User-Only History (Default): Stops feeding AI-generated answers back into the model,
  eliminating autoregressive hallucination snowballing and cross-turn contamination.
- Explicit Constraint Preservation: Retains user-declared negative and equipment
  constraints verbatim (e.g. 'no phone', 'no reception') without assumptions or summarization.
- Short Neutral System Prompt: Reduces hyper-alarmist STOP/CRITICAL emergency templates.
- Strict Context Budgeting: Guards against context overflow within the 1024-token budget.
- Pure LLM Generation: NO RAG, NO retrieval, NO output substitutions, NO hardcoded answers.
- Preserves /reset, /stats, /constraints, /quit, and legacy mode compatibility.
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

DEFAULT_MODEL_PATH = "/Users/kavya/SURVIVE-X-QWEN35-TEST/Qwen3.5-0.8B-Q4_K_M.gguf"
DEFAULT_LLAMA_CLI = "/opt/homebrew/bin/llama-cli"

# Short, neutral system prompt to eliminate hyper-alarmist emergency triggers
DEFAULT_SYSTEM_PROMPT = (
    "You are an offline survival assistant. "
    "Give concise, direct, factual instructions without alarmism."
)


def extract_explicit_constraints(text: str) -> list[str]:
    """
    Extracts explicitly stated constraints verbatim from user text.
    Only captures explicit user declarations of absence or limitations (e.g. 'no phone', 'without a radio').
    Does NOT infer unstated facts, invent assumptions, or summarize.
    """
    found = []
    matches = re.finditer(
        r"\b((?:no|without)\s+[a-zA-Z0-9_\- ]+?)(?:,|\.|\band\b|;|\n|\?|$)",
        text,
        re.IGNORECASE,
    )
    for m in matches:
        phrase = m.group(1).strip()
        words = phrase.split()
        if 2 <= len(words) <= 6:
            norm = phrase[0].upper() + phrase[1:]
            if norm not in found:
                found.append(norm)
    return found


def estimate_tokens(text: str) -> int:
    """Estimates token count conservatively (approx. 3.5 characters per token)."""
    return max(1, int(len(text) / 3.5))


def format_qwen_prompt(
    system_prompt: str,
    user_history: list[str],
    confirmed_constraints: list[str],
    user_input: str,
    mode: str = "user_only",
    legacy_history: list[dict[str, str]] = None,
) -> str:
    """
    Formats the conversation prompt using standard Qwen ChatML template.

    Modes:
    - 'user_only' (Default): Only latest 2-3 user inputs and confirmed constraints are passed.
      Previous AI responses are completely excluded to prevent hallucination snowballing.
    - 'original': Retains legacy alternating user/assistant history for comparison/recovery.
    - 'fresh': Evaluates only the current query in complete isolation.
    """
    if mode == "original" and legacy_history is not None:
        parts = [f"<|im_start|>system\n{system_prompt}<|im_end|>"]
        for turn in legacy_history:
            role = turn.get("role", "user")
            content = turn.get("content", "")
            parts.append(f"<|im_start|>{role}\n{content}<|im_end|>")
        parts.append(f"<|im_start|>user\n{user_input}<|im_end|>")
        parts.append("<|im_start|>assistant\n")
        return "\n".join(parts)

    if mode == "fresh":
        return (
            f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
            f"<|im_start|>user\n{user_input}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )

    # Default: user_only mode
    user_sections = []
    if confirmed_constraints:
        user_sections.append("Confirmed constraints:\n" + "\n".join(f"- {c}" for c in confirmed_constraints))
    if user_history:
        user_sections.append("Previous user context:\n" + "\n".join(f"- {msg}" for msg in user_history))
    user_sections.append(f"Current request:\n{user_input}")

    combined_content = "\n\n".join(user_sections)
    return (
        f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
        f"<|im_start|>user\n{combined_content}<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )


def run_inference(
    prompt: str,
    model_path: str,
    llama_cli: str,
    threads: int = 4,
    ctx_size: int = 1024,
    max_tokens: int = 128,
    temperature: float = 0.2,
    top_p: float = 0.9,
    repeat_penalty: float = 1.15,
    timeout_sec: float = 120,
) -> dict:
    """
    Executes a single CPU-only inference pass using llama-cli.
    Captures stdout for text generation and speed metrics,
    and stderr for peak resident set size (RSS).
    """
    cmd = [
        llama_cli,
        "-m", model_path,
        "-ngl", "0",
        "-t", str(threads),
        "-c", str(ctx_size),
        "-n", str(max_tokens),
        "--temp", str(temperature),
        "--top-p", str(top_p),
        "--repeat-penalty", str(repeat_penalty),
        "--reasoning", "off",
        "-p", prompt,
        "--simple-io",
        "-st",
    ]

    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
        raise RuntimeError('Inference timed out')
    if proc.returncode != 0:
        raise RuntimeError(f'Inference failed (exit {proc.returncode})')
    latency_sec = time.perf_counter() - t0

    # Extract assistant generation cleanly, handling llama-cli prompt display truncation
    if " ... (truncated)\n" in stdout:
        text_after = stdout.split(" ... (truncated)\n", 1)[1]
    elif "... (truncated)\n" in stdout:
        text_after = stdout.split("... (truncated)\n", 1)[1]
    else:
        marker = "<|im_start|>assistant"
        idx = stdout.rfind(marker)
        if idx != -1:
            text_after = stdout[idx + len(marker):]
        else:
            text_after = stdout

    if text_after.startswith("\n"):
        text_after = text_after[1:]

    # Remove trailing timings or exit banner
    gen_text = text_after.split("\n[ Prompt:")[0].split("[ Prompt:")[0].strip()
    if "<|im_end|>" in gen_text:
        gen_text = gen_text.split("<|im_end|>")[0].strip()
    if "</think>" in gen_text:
        gen_text = gen_text.split("</think>")[-1].strip()

    if not gen_text or re.search(r'\x1b|<\|(?:im_|endoftext)|loading model|llama_model_loader:', gen_text, re.I):
        raise RuntimeError('Empty or malformed inference output')

    # Parse speed metrics
    prompt_speed_tps = 0.0
    gen_speed_tps = 0.0
    m_speed = re.search(r"\[ Prompt:\s*([\d.]+)\s*t/s\s*\|\s*Generation:\s*([\d.]+)\s*t/s\s*\]", stdout)
    if m_speed:
        prompt_speed_tps = float(m_speed.group(1))
        gen_speed_tps = float(m_speed.group(2))

    # Parse memory footprint
    m_rss = re.search(r"(\d+)\s+maximum resident set size", stderr)
    m_fp = re.search(r"(\d+)\s+peak memory footprint", stderr)
    peak_rss_mb = int(m_rss.group(1)) / (1024 * 1024) if m_rss else 0.0
    physical_fp_mb = int(m_fp.group(1)) / (1024 * 1024) if m_fp else 0.0

    return {
        "text": gen_text,
        "latency_sec": round(latency_sec, 3),
        "prompt_speed_tps": prompt_speed_tps,
        "gen_speed_tps": gen_speed_tps,
        "peak_rss_mb": round(peak_rss_mb, 2),
        "physical_footprint_mb": round(physical_fp_mb, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="SURVIVE-X Offline Emergency Assistant")
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL_PATH, help="Path to Qwen3.5-0.8B GGUF file")
    parser.add_argument("--llama-cli", default=DEFAULT_LLAMA_CLI, help="Path to llama-cli binary")
    parser.add_argument("-t", "--threads", type=int, default=4, help="CPU threads to use (default: 4)")
    parser.add_argument("-c", "--ctx-size", type=int, default=1024, help="Context size in tokens (default: 1024)")
    parser.add_argument("-n", "--max-tokens", type=int, default=128, help="Maximum generated tokens per response (default: 128)")
    parser.add_argument("--history-limit", type=int, default=3, help="Number of previous user messages to retain (default: 3)")
    parser.add_argument("--sys-prompt", default=DEFAULT_SYSTEM_PROMPT, help="System prompt")
    parser.add_argument(
        "--history-mode",
        choices=["user_only", "original", "fresh"],
        default="user_only",
        help="History handling: 'user_only' (recommended), 'original' (legacy alternating), or 'fresh' (zero history)",
    )
    args = parser.parse_args()

    # Pre-flight check
    if not os.path.exists(args.model):
        sys.stderr.write(f"Error: Model not found at '{args.model}'\n")
        sys.exit(1)
    if not os.path.exists(args.llama_cli):
        sys.stderr.write(f"Error: llama-cli not found at '{args.llama_cli}'\n")
        sys.exit(1)

    # State stores
    user_history: list[str] = []
    confirmed_constraints: list[str] = []
    legacy_history: list[dict[str, str]] = []
    last_stats: dict = {}

    print("=" * 68)
    print(" SURVIVE-X — Offline Emergency Survival Assistant")
    print(" Model: Qwen3.5-0.8B Q4_K_M | Engine: llama.cpp (CPU-Only)")
    print(f" Config: Mode={args.history_mode} | Threads={args.threads} | Context={args.ctx_size} | MaxTokens={args.max_tokens}")
    print(" Commands: /reset (clear history) | /constraints (list) | /stats | /quit")
    print("=" * 68)
    print("⚠️  Disclaimer: Model-only survival advice is experimental and")
    print("   cannot be considered reliable for life-or-death emergencies.")
    print("=" * 68)

    while True:
        try:
            user_input = input("\nUser > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting SURVIVE-X.")
            break

        if not user_input:
            continue

        cmd_lower = user_input.lower()
        if cmd_lower in ("/quit", "/exit", "quit", "exit"):
            print("Exiting SURVIVE-X. Stay safe!")
            break

        if cmd_lower in ("/reset", "reset"):
            user_history.clear()
            confirmed_constraints.clear()
            legacy_history.clear()
            last_stats.clear()
            print("Conversation history and confirmed constraints reset.")
            continue

        if cmd_lower in ("/constraints", "constraints"):
            if not confirmed_constraints:
                print("No explicitly confirmed constraints recorded.")
            else:
                print("Explicitly Confirmed Constraints:")
                for c in confirmed_constraints:
                    print(f"  - {c}")
            continue

        if cmd_lower in ("/help", "help"):
            print("Available commands:")
            print("  /reset       - Clear conversation history and constraints")
            print("  /constraints - Display current confirmed constraints")
            print("  /stats       - Display performance statistics from the last response")
            print("  /quit        - Exit the assistant")
            continue

        if cmd_lower in ("/stats", "stats"):
            if not last_stats:
                print("No inference runs recorded yet.")
            else:
                print("Last Turn Inference Statistics:")
                print(f"  History Mode:       {args.history_mode}")
                print(f"  Latency:            {last_stats.get('latency_sec', 0.0)} s")
                print(f"  Prompt Speed:       {last_stats.get('prompt_speed_tps', 0.0)} tok/s")
                print(f"  Generation Speed:   {last_stats.get('gen_speed_tps', 0.0)} tok/s")
                print(f"  Peak RSS:           {last_stats.get('peak_rss_mb', 0.0)} MB")
                print(f"  Physical Footprint: {last_stats.get('physical_footprint_mb', 0.0)} MB")
                print(f"  User Turns Stored:  {len(user_history)}")
                print(f"  Active Constraints: {len(confirmed_constraints)}")
            continue

        # Extract any new verbatim constraints explicitly declared by user
        candidate_constraints = list(confirmed_constraints)
        candidate_history = list(user_history)
        new_constraints = extract_explicit_constraints(user_input)
        for c in new_constraints:
            if c not in candidate_constraints:
                candidate_constraints.append(c)

        # Build prompt
        prompt = format_qwen_prompt(
            system_prompt=args.sys_prompt,
            user_history=candidate_history,
            confirmed_constraints=candidate_constraints,
            user_input=user_input,
            mode=args.history_mode,
            legacy_history=legacy_history,
        )

        # Context budget guard: prevent silent truncation by pruning oldest user turn if prompt grows too large
        max_allowed_prompt_tokens = args.ctx_size - args.max_tokens - 16
        while candidate_history and estimate_tokens(prompt) > max_allowed_prompt_tokens:
            candidate_history.pop(0)
            prompt = format_qwen_prompt(
                system_prompt=args.sys_prompt,
                user_history=candidate_history,
                confirmed_constraints=candidate_constraints,
                user_input=user_input,
                mode=args.history_mode,
                legacy_history=legacy_history,
            )

        print("\nThinking...", end="", flush=True)
        try:
            result = run_inference(
            prompt=prompt,
            model_path=args.model,
            llama_cli=args.llama_cli,
            threads=args.threads,
            ctx_size=args.ctx_size,
            max_tokens=args.max_tokens,
        )
        except (RuntimeError, OSError) as exc:
            print(f"\rInference error: {exc}")
            continue
        print("\r" + " " * 20 + "\r", end="", flush=True)
        user_history = candidate_history
        confirmed_constraints = candidate_constraints

        assistant_text = result["text"]
        last_stats = result

        print(f"SURVIVE-X > {assistant_text}")
        print(f"\n[{result['prompt_speed_tps']:.1f} p-tok/s | {result['gen_speed_tps']:.1f} g-tok/s | {result['latency_sec']:.2f}s]")

        # Update memory
        # In user_only mode: only user messages are stored; AI answers are NOT appended
        user_history.append(user_input)
        if len(user_history) > args.history_limit:
            user_history.pop(0)

        # For legacy mode recovery
        if args.history_mode == "original":
            legacy_history.append({"role": "user", "content": user_input})
            legacy_history.append({"role": "assistant", "content": assistant_text})
            max_legacy = args.history_limit * 2
            if len(legacy_history) > max_legacy:
                legacy_history = legacy_history[-max_legacy:]


if __name__ == "__main__":
    main()
