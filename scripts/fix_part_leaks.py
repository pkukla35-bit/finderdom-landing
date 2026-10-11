#!/usr/bin/env python3
"""Usuń z artykułów 'przecieki' struktury generowania (odwołania do 'części 1/2') — LLM przepisuje tylko dotknięte akapity."""
import asyncio, glob, json, os, re, sys
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from emergentintegrations.llm.chat import LlmChat, UserMessage

KEY = os.environ["EMERGENT_LLM_KEY"]
PAT = re.compile(r"(części [12]\b|część [12]\b(?!:)|dwóch częściach|drugiej części|pierwszej części|w tej części|w tej odpowiedzi)", re.I)


async def fix_file(f: str):
    s = open(f, encoding="utf-8").read()
    # akapity/lead zawierające przeciek (bez nagłówków typu "Checklista ..., część 1:")
    blocks = [m.group(0) for m in re.finditer(r"<(?:p|div class=\"lead\"|li)>.*?</(?:p|div|li)>", s, re.S) if PAT.search(m.group(0))]
    blocks = [b for b in blocks if "<h2" not in b]
    if not blocks:
        return 0
    chat = LlmChat(api_key=KEY, session_id=f"fix-{os.path.basename(f)}", system_message="Jesteś redaktorem. Zwracasz wyłącznie poprawny JSON.").with_model("anthropic", "claude-sonnet-5-5").with_params(max_tokens=6000, extra_body={"output_config": {"effort": "low"}})
    prompt = (
        "Poniższe fragmenty HTML pochodzą z JEDNEGO ciągłego artykułu. Zawierają odwołania do 'części 1'/'części 2'/'dwóch części', których w artykule NIE MA — to jeden tekst. "
        "Przepisz każdy fragment tak, aby usunąć te odwołania (zamień na naturalne 'poniżej', 'w dalszej części', 'najpierw', albo po prostu usuń zdanie), zachowując resztę treści, znaczenie, tagi HTML i język polski. "
        "Zwróć JSON: {\"fixed\": [\"...\", \"...\"]} — dokładnie tyle elementów, w tej samej kolejności.\n\n"
        + json.dumps(blocks, ensure_ascii=False)
    )
    txt = (await chat.send_message(UserMessage(text=prompt))).strip()
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.S)
    fixed = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])["fixed"]
    if len(fixed) != len(blocks):
        print(f"  ✖ {f}: liczba fragmentów nie zgadza się ({len(fixed)} vs {len(blocks)})")
        return 0
    for old, new in zip(blocks, fixed):
        if PAT.search(new):
            new = PAT.sub("dalej", new)
        s = s.replace(old, new, 1)
    open(f, "w", encoding="utf-8").write(s)
    return len(blocks)


async def main():
    for f in sorted(glob.glob("/app/finderdom-landing/blog/*.html")):
        try:
            n = await fix_file(f)
            if n:
                print(f"✔ {os.path.basename(f)}: {n} fragmentów")
        except Exception as e:
            print(f"✖ {f}: {e}")


asyncio.run(main())
