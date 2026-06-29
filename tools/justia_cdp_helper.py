import argparse
import asyncio
import json
import os
import sys
import urllib.request

import websockets

DEBUG_URL = os.getenv("JUSTIA_CDP_DEBUG_URL", "http://127.0.0.1:9222/json")


class CDPClient:
    def __init__(self, ws_url):
        self.ws_url = ws_url
        self.next_id = 1
        self.ws = None

    async def __aenter__(self):
        self.ws = await websockets.connect(self.ws_url, max_size=20_000_000)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.ws.close()

    async def send(self, method, params=None):
        msg_id = self.next_id
        self.next_id += 1
        await self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            raw = await self.ws.recv()
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(data["error"])
                return data.get("result", {})


def load_targets():
    with urllib.request.urlopen(DEBUG_URL, timeout=10) as response:
        return json.load(response)


def pick_page(
    targets, url_contains="law.justia.com/cases/federal/district-courts/california/cacdce/2026"
):
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    for page in pages:
        if url_contains in page.get("url", ""):
            return page
    return pages[0] if pages else None


async def evaluate(expression):
    target = pick_page(load_targets())
    if not target:
        raise SystemExit("No debuggable Chrome page found on port 9222")
    async with CDPClient(target["webSocketDebuggerUrl"]) as cdp:
        result = await cdp.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        value = result.get("result", {}).get("value")
        print(json.dumps(value, ensure_ascii=False, indent=2))


async def cookies():
    target = pick_page(load_targets())
    if not target:
        raise SystemExit("No debuggable Chrome page found on port 9222")
    async with CDPClient(target["webSocketDebuggerUrl"]) as cdp:
        result = await cdp.send("Network.getAllCookies")
        print(json.dumps(result.get("cookies", []), ensure_ascii=False, indent=2))


async def scrape_cacd_listing(output_path):
    target = pick_page(load_targets())
    if not target:
        raise SystemExit("No debuggable Chrome page found on port 9222")
    expression = r"""
    (async () => {
      const caseLinks = [...document.links]
        .filter(a => a.href.startsWith('https://law.justia.com/cases/federal/district-courts/california/cacdce/'))
        .filter(a => /\/[0-9]+:[0-9]{4}cv[0-9]+\//.test(a.href))
        .filter(a => a.href.endsWith('/'));
      const seen = new Set();
      const rows = [];
      for (const link of caseLinks) {
        const href = link.href;
        if (seen.has(href)) continue;
        seen.add(href);
        const container = link.closest('li, article, div') || link.parentElement;
        const text = (container ? container.innerText : '').replace(/\s+/g, ' ').trim();
        const dateMatch = text.match(/Date:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})/);
        const docketMatch = text.match(/Docket Number:\s*([0-9:]+cv[0-9]+)/i);
        rows.push({
          title: link.innerText.trim(),
          url: href,
          date: dateMatch ? dateMatch[1] : '',
          docket: docketMatch ? docketMatch[1] : ''
        });
      }
      return rows;
    })()
    """
    async with CDPClient(target["webSocketDebuggerUrl"]) as cdp:
        result = await cdp.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in result:
            print(
                json.dumps(result["exceptionDetails"], ensure_ascii=False, indent=2),
                file=sys.stderr,
            )
        rows = result.get("result", {}).get("value") or []
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        for row in rows:
            f.write(
                "\t".join(
                    [
                        row.get("title", ""),
                        row.get("url", ""),
                        row.get("date", ""),
                        row.get("docket", ""),
                    ]
                )
                + "\n"
            )
    print(f"Wrote {len(rows)} rows to {output_path}")


async def scrape_pdf_links(input_path, output_path, limit):
    with open(input_path, "r", encoding="utf-8", errors="ignore") as f:
        urls = []
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[1].startswith("https://law.justia.com/"):
                urls.append(parts[1])
    if limit:
        urls = urls[:limit]

    target = pick_page(load_targets())
    if not target:
        raise SystemExit("No debuggable Chrome page found on port 9222")

    async with CDPClient(target["webSocketDebuggerUrl"]) as cdp:
        results = []
        for idx, url in enumerate(urls, start=1):
            expression = json.dumps(url)
            js = f"""
            (async () => {{
              const url = {expression};
              const html = await fetch(url, {{credentials: 'include'}}).then(r => r.text());
              const doc = new DOMParser().parseFromString(html, 'text/html');
              const pdf = [...doc.links]
                .map(a => a.href)
                .find(h => /cases\\.justia\\.com\\/.*\\.pdf/i.test(h))
                || [...doc.links].map(a => a.href).find(h => /\\/download\\/?$/i.test(h))
                || '';
              const title = doc.querySelector('h1')?.innerText?.trim() || doc.title || '';
              const body = doc.body?.innerText?.replace(/\\s+/g, ' ').trim().slice(0, 20000) || '';
              return {{url, title, pdf, body}};
            }})()
            """
            try:
                result = await cdp.send(
                    "Runtime.evaluate",
                    {"expression": js, "returnByValue": True, "awaitPromise": True},
                )
                value = result.get("result", {}).get("value") or {"url": url, "pdf": ""}
            except Exception as exc:
                value = {"url": url, "pdf": "", "error": str(exc)}
            results.append(value)
            print(f"{idx}/{len(urls)} {url} -> {value.get('pdf') or 'NO PDF'}", flush=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(results)} case details to {output_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["eval", "cookies", "scrape-listing", "scrape-pdfs"])
    parser.add_argument("expression", nargs="?")
    parser.add_argument("--input", default="central_listing_seed.tsv")
    parser.add_argument("--output", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if args.command == "eval":
        if not args.expression:
            raise SystemExit("eval requires a JavaScript expression")
        asyncio.run(evaluate(args.expression))
    elif args.command == "cookies":
        asyncio.run(cookies())
    elif args.command == "scrape-listing":
        asyncio.run(scrape_cacd_listing(args.output or "central_listing_seed.tsv"))
    else:
        asyncio.run(
            scrape_pdf_links(
                args.input, args.output or "central_case_details_from_chrome.json", args.limit
            )
        )


if __name__ == "__main__":
    main()
