import asyncio
import json
import re
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        print("Navigating to TradingView Reliance...")
        try:
            await page.goto("https://in.tradingview.com/symbols/NSE-RELIANCE/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            
            title = await page.title()
            print("Title:", title)
            
            script = """
            () => {
                const results = {};
                // Primary symbol header detection
                const h1 = document.querySelector('h1')?.innerText?.trim() || '';
                results.h1 = h1;
                
                // Find all elements with price classes or test ids
                const candidates = Array.from(document.querySelectorAll('[class*="price"], [class*="last"], [data-field*="price"], [class*="quote"], [class*="ticker"]'))
                    .map(el => ({
                        tag: el.tagName,
                        className: String(el.className),
                        text: el.innerText?.trim()?.slice(0, 100),
                        rect: {
                            top: el.getBoundingClientRect().top,
                            left: el.getBoundingClientRect().left,
                            width: el.getBoundingClientRect().width,
                            height: el.getBoundingClientRect().height
                        }
                    }))
                    .filter(x => x.text && x.text.length < 50);
                
                results.candidates = candidates.slice(0, 20);
                return results;
            }
            """
            data = await page.evaluate(script)
            print("H1:", data.get("h1"))
            print("Candidates found:", len(data.get("candidates", [])))
            for c in data.get("candidates", [])[:10]:
                print(f"  {c['className']} => {c['text']} (pos: top={c['rect']['top']}, left={c['rect']['left']})")
        except Exception as e:
            print("Error:", e)
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
