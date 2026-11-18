"""How fast a host runs the local model: a fresh prompt (nothing cached) and the same conversation with a new
question at the end (what Batsy's prompts look like turn to turn, when the model's cache is reused).

    python -m batsy.tools.bench http://10.10.10.4:11434 [model]
"""
import json
import sys
import time
import urllib.request

CTX = ("[Things you remember]\n- Jonathan is studying 3D design and prints on an Ender-5 Pro.\n- His PC is called bob.\n"
       "[Recent conversation, oldest first]\n" + "\n".join(
           f"20:{10 + i:02d} user: can you check how the print is going and what the bed temperature is right now\n"
           f"20:{10 + i:02d} you: The print is at {20 + i} percent, the bed is at 60 degrees and the nozzle at 190."
           for i in range(14)))


def run(url: str, model: str, head: str, question: str) -> dict:
    body = {"model": model, "stream": False, "keep_alive": "30m",
            "messages": [{"role": "system", "content": head + "You are Batsy, a friendly voice assistant."},
                         {"role": "user", "content": CTX + "\n\n" + question}],
            "options": {"num_predict": 60, "temperature": 0, "seed": 1, "num_ctx": 4096}}
    t = time.time()
    r = json.loads(urllib.request.urlopen(urllib.request.Request(
        url.rstrip("/") + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"}),
        timeout=600).read())
    pd, ed = r["prompt_eval_duration"] / 1e9 or 1e-9, r["eval_duration"] / 1e9 or 1e-9
    return {"total_s": round(time.time() - t, 1), "load_s": round(r.get("load_duration", 0) / 1e9, 1),
            "read_tokens": r["prompt_eval_count"], "reads_per_s": round(r["prompt_eval_count"] / pd, 1),
            "writes_per_s": round(r["eval_count"] / ed, 1)}


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:11434"
    model = sys.argv[2] if len(sys.argv) > 2 else "llama3.2:3b"
    head = f"(bench {time.time()})\n"  # a new start: nothing of this is cached yet
    print("load   ", json.dumps(run(url, model, "", "hi")), flush=True)
    print("fresh  ", json.dumps(run(url, model, head, "Give me three quick tips for stronger 3D prints.")), flush=True)
    print("cached ", json.dumps(run(url, model, head, "And what layer height should I use for a die?")), flush=True)


if __name__ == "__main__":
    main()
