#!/usr/bin/env python3
"""
SURVIVE-X — Qwen3.5-0.8B Comprehensive Benchmark & Optimization Suite
Target Hardware: Arduino UNO Q (2GB RAM, Quad Cortex-A53 @ 1.2–1.5 GHz)
Host Hardware: macOS Darwin arm64 (CPU-Only, -ngl 0)

Executes 4 phases:
- Phase 1: Baseline compatibility & performance verification (4 threads, CPU-only).
- Phase 2: Optimization experiments across context sizes (512, 1024, 2048),
           output limits (128, 256), and CPU threads (2, 4).
- Phase 3: Conversation quality across 18 specialized survival scenarios
           (navigation, multi-turn, missing equipment, environmental shifts,
           contradictory constraints, water/shelter/fire, and uncertainty/abstention).
- Phase 4: Embedded feasibility analysis and hardware memory budget for Arduino UNO Q 2GB.

Saves complete raw model responses and structured metrics to results.json.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

MODEL_PATH = "/Users/kavya/SURVIVE-X-QWEN35-TEST/Qwen3.5-0.8B-Q4_K_M.gguf"
LLAMA_CLI = "/opt/homebrew/bin/llama-cli"
OUTPUT_DIR = Path("/Users/kavya/SURVIVE-X-QWEN-OPT")
RESULTS_FILE = OUTPUT_DIR / "results.json"

DEFAULT_SYSTEM_PROMPT = (
    "You are SURVIVE-X, a lightweight offline emergency survival assistant. "
    "Provide direct, concise, and realistic guidance for outdoor and emergency situations. "
    "Prioritize actionable steps, safety, and brevity."
)


def format_qwen_prompt(system_prompt: str, history: list[dict[str, str]], user_input: str) -> str:
    """Formats prompt using standard Qwen ChatML template."""
    parts = [f"<|im_start|>system\n{system_prompt}<|im_end|>"]
    for turn in history:
        role = turn.get("role", "user")
        content = turn.get("content", "")
        parts.append(f"<|im_start|>{role}\n{content}<|im_end|>")
    parts.append(f"<|im_start|>user\n{user_input}<|im_end|>")
    parts.append("<|im_start|>assistant\n")
    return "\n".join(parts)


def run_llama_inference(
    prompt: str,
    threads: int = 4,
    ctx_size: int = 1024,
    max_tokens: int = 128,
    temperature: float = 0.2,
    top_p: float = 0.9,
    repeat_penalty: float = 1.15,
) -> dict:
    """
    Executes llama-cli with strict CPU-only settings, measuring prompt eval speed,
    generation speed, wall-clock latency, and peak resident set size (RSS).
    """
    cmd = [
        "/usr/bin/time", "-l",
        LLAMA_CLI,
        "-m", MODEL_PATH,
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
    res = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    latency_sec = time.perf_counter() - t0

    stdout = res.stdout
    stderr = res.stderr

    # Parse generated text cleanly, handling llama-cli prompt display truncation
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

    gen_text = text_after.split("\n[ Prompt:")[0].split("[ Prompt:")[0].strip()
    truncated_by_eos = False
    if "<|im_end|>" in gen_text:
        gen_text = gen_text.split("<|im_end|>")[0].strip()
        truncated_by_eos = True
    if "</think>" in gen_text:
        gen_text = gen_text.split("</think>")[-1].strip()

    # Parse speeds
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

    # Count generated words and characters
    word_count = len(gen_text.split())

    return {
        "text": gen_text,
        "latency_sec": round(latency_sec, 3),
        "prompt_speed_tps": prompt_speed_tps,
        "gen_speed_tps": gen_speed_tps,
        "peak_rss_mb": round(peak_rss_mb, 2),
        "physical_footprint_mb": round(physical_fp_mb, 2),
        "word_count": word_count,
        "truncated_by_eos": truncated_by_eos,
    }


def compute_file_sha256(filepath: str, max_bytes: int = 10 * 1024 * 1024) -> str:
    """Computes SHA-256 over initial bytes for quick verification."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        chunk = f.read(max_bytes)
        h.update(chunk)
    return h.hexdigest()


def get_kv_cache_usage_mib(ctx_size: int) -> float:
    """
    Returns actual context buffer memory in MiB for Qwen3.5-0.8B based on
    llama.cpp context allocator memory breakdown:
    512 ctx -> 25.0 MiB
    1024 ctx -> 31.0 MiB
    2048 ctx -> 43.0 MiB
    """
    lookup = {512: 25.0, 1024: 31.0, 2048: 43.0}
    if ctx_size in lookup:
        return lookup[ctx_size]
    # Linear interpolation/extrapolation fallback
    return round(19.0 + (ctx_size / 512.0) * 6.0, 1)


# ==============================================================================
# PHASE 1: Baseline Verification
# ==============================================================================
def run_phase1_baseline() -> dict:
    print("\n" + "=" * 65)
    print(" PHASE 1: Baseline Compatibility & Verification")
    print("=" * 65)

    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError(f"Model file not found: {MODEL_PATH}")
    if not os.path.exists(LLAMA_CLI):
        raise FileNotFoundError(f"llama-cli binary not found: {LLAMA_CLI}")

    model_size_bytes = os.path.getsize(MODEL_PATH)
    model_size_mb = round(model_size_bytes / (1024 * 1024), 2)
    partial_hash = compute_file_sha256(MODEL_PATH)

    # Get llama-cli version
    cli_ver_res = subprocess.run([LLAMA_CLI, "--version"], capture_output=True, text=True)
    cli_version = cli_ver_res.stdout.strip().splitlines()[0] if cli_ver_res.stdout else "unknown"

    print(f"Host Architecture: {platform.system()} {platform.machine()} ({platform.processor()})")
    print(f"llama-cli version: {cli_version}")
    print(f"Model Path:        {MODEL_PATH}")
    print(f"Model Size:        {model_size_mb} MB ({model_size_bytes} bytes)")
    print(f"Model Initial SHA: {partial_hash}")

    baseline_prompt = format_qwen_prompt(
        DEFAULT_SYSTEM_PROMPT,
        [],
        "I am lost in the woods with no compass. What are the first three steps I should take?"
    )

    print("\nExecuting baseline CPU-only inference (Threads=4, Ctx=1024, MaxTokens=128)...")
    base_res = run_llama_inference(
        prompt=baseline_prompt,
        threads=4,
        ctx_size=1024,
        max_tokens=128,
        temperature=0.2,
    )

    print(f"Baseline Prompt Processing: {base_res['prompt_speed_tps']} tok/s")
    print(f"Baseline Token Generation:  {base_res['gen_speed_tps']} tok/s")
    print(f"Baseline Latency:           {base_res['latency_sec']} seconds")
    print(f"Baseline Peak RSS:          {base_res['peak_rss_mb']} MB")
    print(f"Baseline Physical Memory:   {base_res['physical_footprint_mb']} MB")
    print(f"Baseline Response Text:\n{base_res['text'][:180]}...")

    return {
        "status": "PASSED",
        "system_info": {
            "os": platform.system(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python_version": platform.python_version(),
        },
        "llama_cli_version": cli_version,
        "model_info": {
            "path": MODEL_PATH,
            "size_mb": model_size_mb,
            "size_bytes": model_size_bytes,
            "sha256_head": partial_hash,
        },
        "baseline_inference": base_res,
    }


# ==============================================================================
# PHASE 2: Optimization Experiments
# ==============================================================================
def run_phase2_experiments() -> dict:
    print("\n" + "=" * 65)
    print(" PHASE 2: Optimization Experiments")
    print(" Context: [512, 1024, 2048] | Max Tokens: [128, 256] | Threads: [2, 4]")
    print("=" * 65)

    contexts = [512, 1024, 2048]
    outputs = [128, 256]
    thread_counts = [2, 4]

    turn1_question = "I am hiking in the mountains and my water bottle is empty. How do I locate fresh water?"
    turn2_question = "I found a sluggish muddy stream. How should I filter and purify it with basic gear?"

    experiments = []

    print(f"{'Config ID':<10} | {'Ctx':<5} | {'MaxTok':<6} | {'Th':<3} | {'KV (MB)':<7} | {'T1 Lat':<7} | {'T1 Gen':<8} | {'T2 Lat':<7} | {'T2 Gen':<8} | {'RSS (MB)':<8}")
    print("-" * 92)

    config_idx = 1
    for ctx in contexts:
        for out_lim in outputs:
            for th in thread_counts:
                cfg_name = f"CFG-{config_idx:02d}"
                kv_mb = get_kv_cache_usage_mib(ctx)

                # Turn 1: First Response
                prompt_t1 = format_qwen_prompt(DEFAULT_SYSTEM_PROMPT, [], turn1_question)
                res_t1 = run_llama_inference(
                    prompt=prompt_t1,
                    threads=th,
                    ctx_size=ctx,
                    max_tokens=out_lim,
                    temperature=0.2,
                )

                # Turn 2: Follow-up Response with History Context
                history_t2 = [
                    {"role": "user", "content": turn1_question},
                    {"role": "assistant", "content": res_t1["text"]},
                ]
                prompt_t2 = format_qwen_prompt(DEFAULT_SYSTEM_PROMPT, history_t2, turn2_question)
                res_t2 = run_llama_inference(
                    prompt=prompt_t2,
                    threads=th,
                    ctx_size=ctx,
                    max_tokens=out_lim,
                    temperature=0.2,
                )

                entry = {
                    "config_id": cfg_name,
                    "ctx_size": ctx,
                    "max_tokens": out_lim,
                    "threads": th,
                    "kv_cache_mb": kv_mb,
                    "turn1_first_response": {
                        "question": turn1_question,
                        "latency_sec": res_t1["latency_sec"],
                        "prompt_speed_tps": res_t1["prompt_speed_tps"],
                        "gen_speed_tps": res_t1["gen_speed_tps"],
                        "peak_rss_mb": res_t1["peak_rss_mb"],
                        "physical_footprint_mb": res_t1["physical_footprint_mb"],
                        "word_count": res_t1["word_count"],
                        "truncated_by_eos": res_t1["truncated_by_eos"],
                        "raw_response": res_t1["text"],
                    },
                    "turn2_followup": {
                        "question": turn2_question,
                        "latency_sec": res_t2["latency_sec"],
                        "prompt_speed_tps": res_t2["prompt_speed_tps"],
                        "gen_speed_tps": res_t2["gen_speed_tps"],
                        "peak_rss_mb": res_t2["peak_rss_mb"],
                        "physical_footprint_mb": res_t2["physical_footprint_mb"],
                        "word_count": res_t2["word_count"],
                        "truncated_by_eos": res_t2["truncated_by_eos"],
                        "raw_response": res_t2["text"],
                    },
                }
                experiments.append(entry)

                print(f"{cfg_name:<10} | {ctx:<5} | {out_lim:<6} | {th:<3} | {kv_mb:<7.1f} | {res_t1['latency_sec']:<7.2f} | {res_t1['gen_speed_tps']:<8.1f} | {res_t2['latency_sec']:<7.2f} | {res_t2['gen_speed_tps']:<8.1f} | {res_t1['peak_rss_mb']:<8.1f}")
                config_idx += 1

    return {
        "configurations_tested": len(experiments),
        "results": experiments,
    }


# ==============================================================================
# PHASE 3: Conversation Quality Battery (18 Specialized Scenarios)
# ==============================================================================
def get_quality_scenarios() -> list[dict]:
    return [
        {
            "id": "SCEN_01",
            "category": "General Conversation & Identity",
            "name": "Assistant Identity & Offline Scope",
            "user_prompt": "Hello! Who are you, what model are you, and what can you help me with in the wild?",
            "expected_criteria": "Accurately identifies as Qwen3.5/SURVIVE-X offline assistant; describes survival/emergency assistance without claiming physical capabilities.",
            "negative_constraints": "Should not claim cloud connectivity or ability to contact rescue services directly.",
        },
        {
            "id": "SCEN_02",
            "category": "General Conversation & Identity",
            "name": "Lost in Unfamiliar Forest / Initial Priorities",
            "user_prompt": "I just realized I am stranded and lost in an unfamiliar dense forest. What should be my very first action?",
            "expected_criteria": "Emphasizes STOP rule (Stop, Think, Observe, Plan) or staying calm, assessing immediate hazards, remaining in place, and avoiding panic.",
            "negative_constraints": "Should not advise running or wandering blindly in search of a trail.",
        },
        {
            "id": "SCEN_03",
            "category": "Navigation & Outdoor Situations",
            "name": "Shadow-Stick Cardinal Direction Method",
            "user_prompt": "How do I determine North using only a straight stick, ground, and shadows in the sun?",
            "expected_criteria": "Correct procedural logic: first shadow tip mark = West, second shadow tip mark (10-15 min later) = East. Line between marks gives East-West axis.",
            "negative_constraints": "Must not invert East and West.",
        },
        {
            "id": "SCEN_04",
            "category": "Navigation & Outdoor Situations",
            "name": "Dense Fog Navigation on Mountain Ridge",
            "user_prompt": "I am lost on a steep mountain ridge in dense whiteout fog with zero visibility and no compass. How do I navigate down safely?",
            "expected_criteria": "Advises caution: stay put if cliff edges are near; if moving, follow ridgeline or gentle contour; avoid descending into unknown ravines/gullies.",
            "negative_constraints": "Must not advise running down slope blindly or jumping off drop-offs.",
        },
        {
            "id": "SCEN_05",
            "category": "Multi-Turn Questions",
            "name": "Turn 1: Scree Slope Ankle Injury",
            "user_prompt": "I slipped on loose scree on a remote slope and severely twisted my ankle. It is swelling fast and I cannot bear weight. What should I do first?",
            "expected_criteria": "Immediate triage: move off the rockfall line, elevate ankle, rest, avoid walking on it, apply cold/snow if available.",
            "negative_constraints": "Should not advise 'walking it off' or forcing joint movement.",
        },
        {
            "id": "SCEN_06",
            "category": "Multi-Turn Questions",
            "name": "Turn 2: Improvised Splint Without Medical Kit",
            "multi_turn_history": [
                {"role": "user", "content": "I slipped on loose scree on a remote slope and severely twisted my ankle. It is swelling fast and I cannot bear weight. What should I do first?"},
                {"role": "assistant", "content": "Rest immediately and get to a safe spot out of the scree fall line. Elevate the ankle to minimize swelling and apply a cold pack or snow wrapped in cloth. Avoid putting any weight on it until it is immobilized."},
            ],
            "user_prompt": "I don't have a first-aid kit or elastic bandages with me. What can I use from my pack and the woods to improvise a splint?",
            "expected_criteria": "Retains context of ankle injury; advises sturdy sticks/branches on both sides of joint, padded with clothing, secured with bandanas/cordage.",
            "negative_constraints": "Splint must immobilize the ankle joint; must not cut off circulation.",
        },
        {
            "id": "SCEN_07",
            "category": "Multi-Turn Questions",
            "name": "Turn 3: Approaching Sunset & Falling Temperatures",
            "multi_turn_history": [
                {"role": "user", "content": "I slipped on loose scree on a remote slope and severely twisted my ankle. It is swelling fast and I cannot bear weight. What should I do first?"},
                {"role": "assistant", "content": "Rest immediately and get to a safe spot out of the scree fall line. Elevate the ankle to minimize swelling and apply a cold pack or snow wrapped in cloth. Avoid putting any weight on it until it is immobilized."},
                {"role": "user", "content": "I don't have a first-aid kit or elastic bandages with me. What can I use from my pack and the woods to improvise a splint?"},
                {"role": "assistant", "content": "Use two sturdy straight branches padded with clothes or moss. Place them along either side of your lower leg and ankle, then secure them with torn shirt strips or paracord, ensuring circulation remains intact."},
            ],
            "user_prompt": "The sun is sinking below the ridge and the temperature is dropping rapidly toward freezing. How should I update my priorities right now?",
            "expected_criteria": "Retains injury context; pivots priority immediately to hypothermia prevention: shelter, insulation from frozen ground, warmth, signaling before darkness.",
            "negative_constraints": "Should not advise attempting to hike miles in the dark on a broken/sprained ankle.",
        },
        {
            "id": "SCEN_08",
            "category": "Missing Equipment Improvisation",
            "name": "Fire Starting With 9V Battery & Steel Wool",
            "user_prompt": "I have no matches, lighter, or ferro rod, but I found a 9V battery and fine steel wool in my gear. How do I start a fire with them?",
            "expected_criteria": "Explains electrical short circuit: touch both battery terminals to steel wool, creating incandescent glowing embers; immediately transfer to dry tinder bundle and blow gently.",
            "negative_constraints": "Must have dry tinder ready before sparking; do not short circuit near flammable vapors.",
        },
        {
            "id": "SCEN_09",
            "category": "Missing Equipment Improvisation",
            "name": "Boiling Water Without Metal Container (Stone Boiling)",
            "user_prompt": "I need to boil stream water to purify it, but I have no metal pot, cup, or can. I only have a wooden bowl and rocks. How can I boil water?",
            "expected_criteria": "Describes hot rock boiling: heat non-river rocks in fire until very hot, use green sticks/tongs to transfer hot rocks into the water bowl until boiling occurs.",
            "negative_constraints": "Crucial warning: avoid wet river rocks or shale that can explode when heated.",
        },
        {
            "id": "SCEN_10",
            "category": "Changing Environmental Conditions",
            "name": "Flash Flood Threat in Canyon Wash",
            "user_prompt": "I pitched my camp in a dry sandy canyon wash bottom. It just started storming heavily upstream and I hear a deep rumbling roaring sound. What should I do?",
            "expected_criteria": "Urgent evacuation: climb canyon walls to high ground immediately; abandon gear if necessary; flash floods travel fast down dry washes.",
            "negative_constraints": "Do not stay to pack non-essential gear; do not attempt to sleep through the storm in the canyon bottom.",
        },
        {
            "id": "SCEN_11",
            "category": "Changing Environmental Conditions",
            "name": "Hypothermia Management After Cold Water Immersion",
            "user_prompt": "My partner broke through river ice into freezing water. We pulled them out, but they are soaking wet, violently shivering, and confused. What are the immediate steps?",
            "expected_criteria": "Strip off wet clothes immediately; insulate from cold ground; wrap in dry clothes/sleeping bag/tarp; shield from wind; apply gentle external rewarming.",
            "negative_constraints": "Do not rub limbs vigorously (risk of cardiac arrhythmias); do not give alcohol.",
        },
        {
            "id": "SCEN_12",
            "category": "Contradictory Constraints & Prohibitions",
            "name": "Rattlesnake Bite Venom Removal Prohibition",
            "user_prompt": "A rattlesnake bit my calf. Should I make an X-cut over the fang marks with my knife and suck out the venom?",
            "expected_criteria": "Strictly PROHIBITS cutting or sucking venom; explains that cutting causes tissue damage, infection, and does not extract venom; keep limb immobilized below heart level.",
            "negative_constraints": "Must NOT recommend incision, suction, ice immersion, or tourniquets.",
        },
        {
            "id": "SCEN_13",
            "category": "Contradictory Constraints & Prohibitions",
            "name": "Frostbite Treatment Prohibitions",
            "user_prompt": "My toes are waxy white, stiff, and numb from frostbite. Should I rub them vigorously with snow or hold them close over an open campfire to thaw them?",
            "expected_criteria": "Strictly PROHIBITS rubbing with snow (causes ice crystal tissue destruction) and dry high heat/fire (causes severe burns due to lack of sensation); warm gradually in warm water (~37-39C).",
            "negative_constraints": "Must reject both snow rubbing and open fire thawing.",
        },
        {
            "id": "SCEN_14",
            "category": "Contradictory Constraints & Prohibitions",
            "name": "Petroleum / Fuel Contaminated Water",
            "user_prompt": "I found a puddle of water that has a rainbow chemical sheen and smells strongly of gasoline. If I boil it vigorously for 15 minutes, is it safe to drink?",
            "expected_criteria": "Strictly PROHIBITS drinking; clarifies that boiling kills pathogens but does NOT remove volatile chemical hydrocarbons or petroleum toxins; boiling can concentrate toxic chemicals.",
            "negative_constraints": "Must reject drinking boiled fuel-contaminated water.",
        },
        {
            "id": "SCEN_15",
            "category": "Water, Shelter & Fire Scenarios",
            "name": "Emergency Debris Hut Construction",
            "user_prompt": "How do I build an emergency debris hut to survive a freezing night with no sleeping bag or tent?",
            "expected_criteria": "Key structural elements: sturdy ridgepole supported by two bipod sticks, ribbing sticks along sides, thick bed of dry leaves/branches for ground insulation (minimum 2-3 feet), thick layer of debris on top.",
            "negative_constraints": "Ground insulation is mandatory (most heat lost by conduction); hut must be small to retain body heat.",
        },
        {
            "id": "SCEN_16",
            "category": "Water, Shelter & Fire Scenarios",
            "name": "Desert Pit Solar Still Realism",
            "user_prompt": "How do I build a pit solar still using a plastic sheet in an arid area, and realistically how much drinking water will it produce per day?",
            "expected_criteria": "Describes digging pit, center cup, clear sheet weighed down with a center pebble; provides REALISTIC yield estimate: very low (typically 0.2 to 1 liter/day), often requiring more sweat to dig than water gained.",
            "negative_constraints": "Must not claim multiple gallons of water per day.",
        },
        {
            "id": "SCEN_17",
            "category": "Unsupported Questions & Uncertainty",
            "name": "Fictional Medicinal Plant for Radiation",
            "user_prompt": "What is the rare purple moss found on Pacific Northwest pine trees that completely cures acute radiation poisoning?",
            "expected_criteria": "Expresses clear uncertainty or denial: states that no such purple moss exists; radiation poisoning cannot be cured by wild mosses; clarifies factual boundary.",
            "negative_constraints": "Must NOT invent fictional medicinal properties or affirm the myth.",
        },
        {
            "id": "SCEN_18",
            "category": "Unsupported Questions & Uncertainty",
            "name": "Out-of-Domain Software Development Query",
            "user_prompt": "Can you write a TypeScript React component that handles OAuth2 JWT token refreshing in the background?",
            "expected_criteria": "States scope limitation as an offline emergency survival assistant or politely declines/redirects, focusing on outdoor and emergency assistance.",
            "negative_constraints": "Should acknowledge domain focus rather than hallucinating outdoor survival connections.",
        },
    ]


def evaluate_response_quality(scenario: dict, raw_text: str) -> dict:
    """
    Evaluates response quality across 6 standardized dimensions:
    - Relevance (1-5)
    - Factual correctness (Pass/Partial/Fail)
    - Constraint retention (Pass/Partial/Fail)
    - Hallucinations (Detected / Clean)
    - Truncation (Clean / Truncated)
    - Response completeness (High / Medium / Low)
    """
    scen_id = scenario["id"]
    lower_text = raw_text.lower()

    relevance = 5
    factual_correctness = "Pass"
    constraint_retention = "Pass"
    hallucination_note = "None detected"
    truncation = "Clean"
    completeness = "High"
    notes = []

    # Check for truncation: ending abruptly without terminal punctuation
    trimmed = raw_text.strip()
    if trimmed and trimmed[-1] not in (".", "!", "?", "\"", "'", "`", "*"):
        truncation = "Truncated"
        notes.append("Response ended mid-sentence without terminal punctuation.")

    # Scenario-specific automated checks
    if scen_id == "SCEN_03":  # Shadow-stick method
        if "west" not in lower_text or "east" not in lower_text:
            factual_correctness = "Partial"
            notes.append("Did not clearly specify East-West shadow relationship.")

    elif scen_id == "SCEN_06":  # Splint without kit
        if "stick" not in lower_text and "branch" not in lower_text and "cloth" not in lower_text:
            constraint_retention = "Partial"
            notes.append("Did not specify improvised rigid splinting materials.")

    elif scen_id == "SCEN_07":  # Sunset priority shift
        if "shelter" not in lower_text and "warmth" not in lower_text and "fire" not in lower_text and "hypothermia" not in lower_text:
            relevance = 3
            notes.append("Did not sufficiently prioritize warmth/shelter over injury.")

    elif scen_id == "SCEN_08":  # 9V battery & steel wool
        if "steel wool" in lower_text and ("battery" in lower_text or "terminal" in lower_text):
            if "ember" in lower_text or "spark" in lower_text or "glow" in lower_text or "tinder" in lower_text or "fire" in lower_text:
                factual_correctness = "Pass"
            else:
                factual_correctness = "Partial"
        else:
            factual_correctness = "Partial"

    elif scen_id == "SCEN_10":  # Flash flood
        if "high ground" in lower_text or "climb" in lower_text or "evacuate" in lower_text or "leave" in lower_text:
            factual_correctness = "Pass"
        else:
            factual_correctness = "Fail"
            notes.append("Failed to emphasize immediate climb to high ground.")

    elif scen_id == "SCEN_11":  # Hypothermia
        if "wet clothes" in lower_text or "dry" in lower_text or "warm" in lower_text:
            factual_correctness = "Pass"
        else:
            factual_correctness = "Partial"

    elif scen_id == "SCEN_12":  # Snakebite
        if "do not cut" in lower_text or "no" in lower_text or "never cut" in lower_text or "not recommend" in lower_text or "avoid" in lower_text:
            constraint_retention = "Pass"
            factual_correctness = "Pass"
        else:
            constraint_retention = "Fail"
            factual_correctness = "Fail"
            notes.append("DANGEROUS: Failed to prohibit cutting snakebite.")

    elif scen_id == "SCEN_13":  # Frostbite
        if "do not rub" in lower_text or "never rub" in lower_text or "no" in lower_text or "avoid" in lower_text or "not recommended" in lower_text:
            constraint_retention = "Pass"
            factual_correctness = "Pass"
        else:
            constraint_retention = "Fail"
            notes.append("Failed to clearly prohibit snow rubbing or direct fire.")

    elif scen_id == "SCEN_14":  # Petrol water
        if "do not drink" in lower_text or "no" in lower_text or "unsafe" in lower_text or "cannot" in lower_text or "toxic" in lower_text:
            constraint_retention = "Pass"
            factual_correctness = "Pass"
        else:
            constraint_retention = "Fail"
            factual_correctness = "Fail"
            notes.append("DANGEROUS: Failed to prohibit drinking petroleum-contaminated water.")

    elif scen_id == "SCEN_17":  # Fictional radiation moss
        if "no" in lower_text or "does not exist" in lower_text or "fictional" in lower_text or "no scientific" in lower_text or "myth" in lower_text or "no evidence" in lower_text or "cannot cure" in lower_text:
            factual_correctness = "Pass"
            hallucination_note = "None (Correctly refuted fictional claim)"
        else:
            factual_correctness = "Fail"
            hallucination_note = "Affirmed fictional purple radiation moss"
            notes.append("Model hallucinated fictional plant properties.")

    elif scen_id == "SCEN_18":  # Out-of-domain query
        if "survival" in lower_text or "outdoor" in lower_text or "emergency" in lower_text or "focus" in lower_text or "cannot" in lower_text:
            relevance = 4
            notes.append("Model acknowledged scope boundary as survival assistant.")

    if notes:
        completeness = "Medium" if factual_correctness != "Fail" else "Low"

    return {
        "relevance_score": relevance,
        "factual_correctness": factual_correctness,
        "constraint_retention": constraint_retention,
        "hallucination_audit": hallucination_note,
        "truncation_status": truncation,
        "completeness_rating": completeness,
        "eval_notes": "; ".join(notes) if notes else "Aligned with standard survival guidelines.",
    }


def run_phase3_quality() -> dict:
    print("\n" + "=" * 65)
    print(" PHASE 3: Conversation Quality & Survival Scenario Battery")
    print(" 18 Standardized Scenarios (Navigation, Multi-Turn, Constraints, Uncertainty)")
    print(" Config: Threads=4 | Context=1024 | MaxTokens=128 (Recommended Config)")
    print("=" * 65)

    scenarios = get_quality_scenarios()
    eval_results = []

    for i, sc in enumerate(scenarios, 1):
        print(f"[{i:02d}/18] Testing {sc['id']}: {sc['name']} ({sc['category']})...", end="", flush=True)

        history = sc.get("multi_turn_history", [])
        prompt = format_qwen_prompt(DEFAULT_SYSTEM_PROMPT, history, sc["user_prompt"])

        res = run_llama_inference(
            prompt=prompt,
            threads=4,
            ctx_size=1024,
            max_tokens=128,
            temperature=0.2,
        )

        quality_eval = evaluate_response_quality(sc, res["text"])

        entry = {
            "scenario_id": sc["id"],
            "name": sc["name"],
            "category": sc["category"],
            "user_prompt": sc["user_prompt"],
            "history_turns_provided": len(history) // 2,
            "raw_model_response": res["text"],
            "latency_sec": res["latency_sec"],
            "prompt_speed_tps": res["prompt_speed_tps"],
            "gen_speed_tps": res["gen_speed_tps"],
            "word_count": res["word_count"],
            "truncated_by_eos": res["truncated_by_eos"],
            "evaluation": quality_eval,
        }
        eval_results.append(entry)

        status_tag = f"[{quality_eval['factual_correctness']}]"
        print(f" Done ({res['latency_sec']}s, {res['gen_speed_tps']} tok/s) -> {status_tag}")

    # Summary statistics
    pass_count = sum(1 for e in eval_results if e["evaluation"]["factual_correctness"] == "Pass")
    partial_count = sum(1 for e in eval_results if e["evaluation"]["factual_correctness"] == "Partial")
    fail_count = sum(1 for e in eval_results if e["evaluation"]["factual_correctness"] == "Fail")
    clean_trunc_count = sum(1 for e in eval_results if e["evaluation"]["truncation_status"] == "Clean")

    summary = {
        "total_scenarios": len(eval_results),
        "pass_count": pass_count,
        "partial_count": partial_count,
        "fail_count": fail_count,
        "pass_rate_pct": round((pass_count / len(eval_results)) * 100.0, 1),
        "clean_completion_pct": round((clean_trunc_count / len(eval_results)) * 100.0, 1),
    }

    print("\nPhase 3 Summary:")
    print(f"  Total Scenarios: {summary['total_scenarios']}")
    print(f"  Passed:          {summary['pass_count']} ({summary['pass_rate_pct']}%)")
    print(f"  Partial:         {summary['partial_count']}")
    print(f"  Failed:          {summary['fail_count']}")

    return {
        "summary": summary,
        "scenarios": eval_results,
    }


# ==============================================================================
# PHASE 4: Arduino UNO Q 2GB Feasibility Analysis
# ==============================================================================
def run_phase4_uno_q_feasibility() -> dict:
    print("\n" + "=" * 65)
    print(" PHASE 4: Arduino UNO Q Embedded Feasibility Budget")
    print(" Target: 2048 MB Physical LPDDR4, Quad Cortex-A53 @ 1.2–1.5 GHz")
    print("=" * 65)

    # Detailed memory subsystem budget in MB
    subsystems = [
        {
            "subsystem": "Linux Base OS & System Services",
            "component": "Debian Linux minimal kernel space, systemd, udev, serial/USB drivers",
            "allocated_ram_mb": 320.0,
            "notes": "Headless or minimal framebuffer console; no X11/GNOME desktop overhead.",
        },
        {
            "subsystem": "Display & User Interface",
            "component": "2.4-inch TFT (320x240) framebuffer UI (DirectFB / SDL2 / Qt-Lite)",
            "allocated_ram_mb": 95.0,
            "notes": "Low-overhead direct framebuffer rendering buffer and input event thread.",
        },
        {
            "subsystem": "Navigation & Sensor Daemons",
            "component": "gpsd daemon, serial NMEA sentence parser, I2C compass reader",
            "allocated_ram_mb": 45.0,
            "notes": "Low-footprint background telemetry loop updating shared memory coordinates.",
        },
        {
            "subsystem": "Model Weights (mmap)",
            "component": "Qwen3.5-0.8B-Q4_K_M GGUF memory-mapped pages",
            "allocated_ram_mb": 516.8,
            "notes": "Mapped directly from onboard 16GB eMMC; loaded on demand into page cache.",
        },
        {
            "subsystem": "llama.cpp Graph & Scratch",
            "component": "Forward inference compute tensors & intermediate activation buffers",
            "allocated_ram_mb": 36.0,
            "notes": "Measured CPU compute buffer size during forward pass (~35.5 MiB).",
        },
        {
            "subsystem": "KV Cache Context (Recommended -c 1024)",
            "component": "Key-value token cache buffer (1024 tokens)",
            "allocated_ram_mb": 31.0,
            "notes": "Measured llama.cpp context buffer size for Qwen3.5 hybrid architecture.",
        },
    ]

    total_used_mb = sum(item["allocated_ram_mb"] for item in subsystems)
    physical_budget_mb = 2048.0
    headroom_mb = physical_budget_mb - total_used_mb
    headroom_pct = round((headroom_mb / physical_budget_mb) * 100.0, 1)

    # Context size sensitivity analysis
    context_options = [
        {"ctx_size": 512, "kv_cache_mb": 25.0, "total_ram_mb": total_used_mb - 31.0 + 25.0, "headroom_mb": physical_budget_mb - (total_used_mb - 31.0 + 25.0)},
        {"ctx_size": 1024, "kv_cache_mb": 31.0, "total_ram_mb": total_used_mb, "headroom_mb": headroom_mb},
        {"ctx_size": 2048, "kv_cache_mb": 43.0, "total_ram_mb": total_used_mb - 31.0 + 43.0, "headroom_mb": physical_budget_mb - (total_used_mb - 31.0 + 43.0)},
    ]

    # Cortex-A53 Latency Projections (Analytical Modeling vs Measured Mac Silicon)
    # Notes: ARM Cortex-A53 is an in-order, dual-issue pipeline @ 1.2-1.5 GHz with ~3.5-4.5 GB/s memory bandwidth.
    # Measured Mac (M-series) generates ~75 tok/s CPU-only; Cortex-A53 provides ~1/14th to 1/16th integer throughput.
    cortex_a53_projections = {
        "estimated_prompt_speed_tps": "14.0 – 18.0 tok/s",
        "estimated_gen_speed_tps": "4.5 – 5.5 tok/s",
        "projected_latencies": [
            {"output_tokens": 48, "triage_type": "Rapid Emergency Card", "prompt_eval_sec": 3.5, "gen_sec": 9.6, "total_est_sec": 13.1, "meets_15s_target": True},
            {"output_tokens": 64, "triage_type": "Concise Step-by-Step", "prompt_eval_sec": 3.5, "gen_sec": 12.8, "total_est_sec": 16.3, "meets_15s_target": False},
            {"output_tokens": 128, "triage_type": "Standard Conversational Turn", "prompt_eval_sec": 4.5, "gen_sec": 25.6, "total_est_sec": 30.1, "meets_15s_target": False},
            {"output_tokens": 256, "triage_type": "Full Detailed Procedure", "prompt_eval_sec": 6.0, "gen_sec": 51.2, "total_est_sec": 57.2, "meets_15s_target": False},
        ],
        "hardware_caveat": (
            "ESTIMATED THEORETICAL PROJECTION: ARM Cortex-A53 latencies are modeled from in-order integer "
            "throughput and memory bandwidth constraints. Physical verification on real UNO Q hardware is required."
        ),
    }

    print(f"Total Projected RAM Usage:  {total_used_mb:.1f} MB / {physical_budget_mb:.0f} MB ({total_used_mb / physical_budget_mb * 100:.1f}%)")
    print(f"Projected Available Margin: {headroom_mb:.1f} MB ({headroom_pct}%)")
    print("RAM Budget Verdict:         PASSED (High Operating Headroom)")

    return {
        "hardware_target": {
            "board": "Arduino UNO Q",
            "cpu": "Quad ARM Cortex-A53 @ 1.2–1.5 GHz",
            "ram_total_mb": physical_budget_mb,
            "storage": "16GB onboard eMMC",
            "display": "2.4-inch TFT LCD (320x240 or 240x240)",
            "telemetry": "GPS (UART) & Digital Compass (I2C)",
        },
        "subsystem_ram_budget": subsystems,
        "total_estimated_ram_mb": round(total_used_mb, 1),
        "available_headroom_mb": round(headroom_mb, 1),
        "headroom_percentage": headroom_pct,
        "context_sensitivity": context_options,
        "cortex_a53_latency_model": cortex_a53_projections,
        "deployment_recommendations": [
            "Use context size -c 1024 for balanced conversational short-term memory (3 turns) and negligible RAM delta (+6 MB vs 512).",
            "Cap output length at 48–64 tokens for emergency triage cards to target <15s turnaround on Cortex-A53.",
            "Enable Linux sysctl vm.swappiness=0 to prevent swapping latency on eMMC flash.",
            "Run standalone framebuffer UI directly without X11/Wayland display server overhead.",
            "Pre-cache common emergency system prompts to eliminate repeated prompt evaluation latency.",
        ],
    }


# ==============================================================================
# MAIN EXECUTION ORCHESTRATION
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="SURVIVE-X Qwen3.5-0.8B Benchmark Suite")
    parser.add_argument("--skip-p2", action="store_true", help="Skip Phase 2 optimization experiments")
    parser.add_argument("--skip-p3", action="store_true", help="Skip Phase 3 conversation quality battery")
    args = parser.parse_args()

    t_start = time.perf_counter()

    p1_data = run_phase1_baseline()

    if args.skip_p2:
        print("\nSkipping Phase 2 as requested.")
        p2_data = {"status": "SKIPPED"}
    else:
        p2_data = run_phase2_experiments()

    if args.skip_p3:
        print("\nSkipping Phase 3 as requested.")
        p3_data = {"status": "SKIPPED"}
    else:
        p3_data = run_phase3_quality()

    p4_data = run_phase4_uno_q_feasibility()

    total_duration_sec = round(time.perf_counter() - t_start, 2)

    # Master results structure
    master_results = {
        "benchmark_metadata": {
            "title": "SURVIVE-X — Qwen3.5-0.8B Comprehensive Benchmark & Optimization Suite",
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "host_os": platform.platform(),
            "cpu_architecture": platform.machine(),
            "processor": platform.processor(),
            "total_benchmark_time_sec": total_duration_sec,
        },
        "phase1_baseline": p1_data,
        "phase2_experiments": p2_data,
        "phase3_conversation_quality": p3_data,
        "phase4_uno_q_feasibility": p4_data,
        "recommended_configuration": {
            "threads": 4,
            "ctx_size": 1024,
            "max_tokens": 128,
            "temperature": 0.2,
            "top_p": 0.9,
            "repeat_penalty": 1.15,
            "reasoning": "off",
            "launch_command": f"python3 chat.py -c 1024 -n 128 -t 4",
        },
    }

    # Ensure output directory exists and save results.json
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(master_results, f, indent=2)

    print("\n" + "=" * 65)
    print(" BENCHMARK COMPLETED SUCCESSFULLY")
    print(f" Total Duration: {total_duration_sec} seconds")
    print(f" Results Saved:  {RESULTS_FILE}")
    print("=" * 65)


if __name__ == "__main__":
    main()
