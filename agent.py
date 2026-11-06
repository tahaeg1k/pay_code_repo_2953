"""ScopePay shopping agent: Claude shops at a demo store and pays with a scoped ScopePay code.

The code is held by this runner and injected into tool calls, so the model never sees it, let alone the
payer's PayPal. Run with the ScopePay server up (./PayCodes.command or uvicorn app:app --port 8001):

  python agent.py --code SP-XXXX-XXXX-XXXX "Weekly vegetarian groceries for two, about £15"
  python agent.py --code SP-... --no-precheck "Buy everything for a big breakfast"   # shows a decline + recovery
"""
import argparse
import json
import sys

import agent_tools
import ai

SYSTEM = """You are a shopping agent for a person in the UK. They gave you a ScopePay code: a payment code with \
its own spending cap, expiry, optional shop lock and an optional intent written by the owner. The code is kept by \
your tools; you never see it and you never see the owner's PayPal.

How to work:
1. If check_code is available, call it first and respect what it says: shop, money remaining, intent.
2. Look at the shop's products and pick a basket that fits the request and the intent, within the remaining budget.
3. Pay once with pay_with_code.
4. If the payment is declined, read the reason, adjust the basket once, and try again. Never try to get around a limit.
5. If the payment is held, stop: tell the person it is waiting for their approval in the ScopePay wallet.
Finish with a short summary for the person: what you bought, the total, the outcome and the evidence id."""


def run(task, code, client=None, precheck=True, max_turns=10, log=print):
    """Run the agent. Returns {"final": text, "steps": [...]} so a UI can show the whole trace."""
    client = client or agent_tools.ScopePayClient()
    ai.refresh()
    tools = agent_tools.schemas(with_code=False, skip=() if precheck else ("check_code",))
    messages, steps = [{"role": "user", "content": task}], []
    for _ in range(max_turns):
        res = ai.messages(model=ai.AGENT_MODEL, max_tokens=2000, system=SYSTEM, tools=tools, messages=messages)
        messages.append({"role": "assistant", "content": res["content"]})
        for block in res["content"]:
            if block.get("type") == "text" and block["text"].strip():
                steps.append({"kind": "say", "text": block["text"]}); log(f"\n🤖 {block['text']}")
        if res.get("stop_reason") != "tool_use":
            return {"final": "\n".join(s["text"] for s in steps if s["kind"] == "say")[-2000:], "steps": steps}
        results = []
        for block in (b for b in res["content"] if b.get("type") == "tool_use"):
            out = agent_tools.run(client, block["name"], block["input"], code=code)
            steps.append({"kind": "tool", "name": block["name"], "input": block["input"], "output": out})
            log(f"\n🔧 {block['name']}({json.dumps(block['input'], ensure_ascii=False)})\n   → {json.dumps(out, ensure_ascii=False)[:400]}")
            results.append({"type": "tool_result", "tool_use_id": block["id"], "content": agent_tools.as_text(out),
                            "is_error": bool(out.get("error")) if isinstance(out, dict) else False})
        messages.append({"role": "user", "content": results})
    steps.append({"kind": "say", "text": "Stopped: too many steps."})
    return {"final": "Stopped: too many steps.", "steps": steps}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task", help="What to buy, in plain English")
    ap.add_argument("--code", required=True, help="ScopePay code issued to the agent")
    ap.add_argument("--no-precheck", action="store_true", help="Hide check_code, to show a decline and recovery")
    a = ap.parse_args()
    if not ai.available():
        sys.exit("Set OPENROUTER_API_KEY (or ANTHROPIC_API_KEY) in .env first.")
    run(a.task, a.code, precheck=not a.no_precheck)


if __name__ == "__main__":
    main()
