"""
Accept Togal EUSA via Playwright (Edge) — target the styled checkbox wrapper.
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

AUTH_PATH = Path(r"C:\Agent Carol\data\config\togal_auth.json")
DRAWING_PATH = Path(r"C:\Agent Carol\data\projects\hopewell_elementary_gym\bid_docs\CD Set-Hopewell Elem Ph 2 Gym.pdf")

async def main():
    auth = json.loads(AUTH_PATH.read_text())
    project_id = auth.get("project_id")
    set_id = auth.get("set_id")

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=False, slow_mo=300)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()

        print("[1] Navigating to Togal login...")
        await page.goto("https://app.togal.ai/auth/login", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

        await page.fill('#email', auth["email"])
        await page.fill('#password', auth["password"])
        await page.wait_for_timeout(500)

        print("[2] Clicking Login...")
        await page.click('#login')
        await page.wait_for_timeout(5000)
        print(f"[3] URL: {page.url}")

        # Analyze the EUSA checkbox DOM
        dom_info = await page.evaluate("""(() => {
            const cb = document.querySelector('input[type="checkbox"]');
            if (!cb) return {error: 'no checkbox'};

            const parent = cb.parentElement;
            const grandparent = parent ? parent.parentElement : null;

            // Find all Checkbox-related styled elements
            const checkboxEls = document.querySelectorAll('[class*="Checkbox"]');
            const elInfo = Array.from(checkboxEls).map(c => ({
                tag: c.tagName,
                cls: (c.getAttribute('class') || '').substring(0, 80),
                visible: c.offsetHeight > 0 && c.offsetWidth > 0,
                w: c.getBoundingClientRect().width,
                h: c.getBoundingClientRect().height,
            }));

            return {
                cb_class: cb.getAttribute('class') || '',
                cb_checked: cb.checked,
                cb_rect: cb.getBoundingClientRect(),
                parent_tag: parent ? parent.tagName : null,
                parent_class: parent ? (parent.getAttribute('class') || '').substring(0, 80) : null,
                parent_rect: parent ? parent.getBoundingClientRect() : null,
                gp_tag: grandparent ? grandparent.tagName : null,
                gp_class: grandparent ? (grandparent.getAttribute('class') || '').substring(0, 80) : null,
                checkbox_elements: elInfo
            };
        })()""")
        print(f"[4] DOM:\n{json.dumps(dom_info, indent=2)}")

        # Click each visible Checkbox-styled element
        click_result = await page.evaluate("""(() => {
            const results = [];
            const checkboxEls = document.querySelectorAll('[class*="Checkbox"]');
            checkboxEls.forEach((el, i) => {
                if (el.tagName !== 'INPUT' && el.offsetHeight > 0 && el.offsetWidth > 0) {
                    el.scrollIntoView({block: 'center'});
                    el.click();
                    results.push({i, tag: el.tagName, cls: (el.getAttribute('class') || '').substring(0, 60)});
                }
            });

            // Also try: click parent of checkbox input
            const cb = document.querySelector('input[type="checkbox"]');
            if (cb && cb.parentElement) {
                cb.parentElement.scrollIntoView({block: 'center'});
                cb.parentElement.click();
                results.push({special: 'parent_click'});
            }

            // Try React onChange on the checkbox
            if (cb) {
                const propsKey = Object.keys(cb).find(k => k.startsWith('__reactProps$'));
                if (propsKey) {
                    const props = cb[propsKey];
                    if (props.onChange) {
                        props.onChange({target: {checked: true, type: 'checkbox'}});
                        results.push({special: 'react_onChange', keys: Object.keys(props).join(',')});
                    } else {
                        results.push({special: 'no_onChange', keys: Object.keys(props).join(',')});
                    }
                }
            }

            // Check the checkbox state after all clicks
            const checked = cb ? cb.checked : null;
            return {results, checked};
        })()""")
        print(f"[5] Clicks:\n{json.dumps(click_result, indent=2)}")
        await page.wait_for_timeout(1000)

        # Check Continue button
        btn_state = await page.evaluate("""(() => {
            const btns = Array.from(document.querySelectorAll('button'));
            const btn = btns.find(b => b.textContent.trim() === 'Continue');
            if (!btn) return {error: 'no Continue button'};
            return {disabled: btn.disabled, text: btn.textContent.trim()};
        })()""")
        print(f"[6] Continue: {json.dumps(btn_state)}")

        if isinstance(btn_state, dict) and not btn_state.get('disabled', True):
            await page.click('button:has-text("Continue")')
            print("[7] Clicked Continue!")
            await page.wait_for_timeout(5000)
        else:
            # Try: simulate full user interaction — mouse click on the visible checkbox SVG/span
            print("[7] Trying mouse click on checkbox area...")

            # Get position of the checkbox container
            pos = await page.evaluate("""(() => {
                const checkboxEls = document.querySelectorAll('[class*="Checkbox"]');
                for (const el of checkboxEls) {
                    if (el.tagName !== 'INPUT' && el.offsetHeight > 0) {
                        const r = el.getBoundingClientRect();
                        return {x: r.x + r.width/2, y: r.y + r.height/2, w: r.width, h: r.height};
                    }
                }
                // Fallback: parent of input
                const cb = document.querySelector('input[type="checkbox"]');
                if (cb && cb.parentElement) {
                    const r = cb.parentElement.getBoundingClientRect();
                    return {x: r.x + r.width/2, y: r.y + r.height/2, w: r.width, h: r.height};
                }
                return null;
            })()""")
            print(f"    Click target: {json.dumps(pos)}")

            if pos and pos.get('x'):
                # Use Playwright's mouse to physically click the coordinates
                await page.mouse.click(pos['x'], pos['y'])
                print(f"    Mouse clicked at ({pos['x']}, {pos['y']})")
                await page.wait_for_timeout(1000)

                # Check state again
                state = await page.evaluate("""(() => {
                    const cb = document.querySelector('input[type="checkbox"]');
                    const btns = Array.from(document.querySelectorAll('button'));
                    const btn = btns.find(b => b.textContent.trim() === 'Continue');
                    return {
                        checked: cb ? cb.checked : null,
                        continue_disabled: btn ? btn.disabled : null
                    };
                })()""")
                print(f"    State after mouse click: {json.dumps(state)}")

                if isinstance(state, dict) and not state.get('continue_disabled', True):
                    await page.click('button:has-text("Continue")')
                    print("    Clicked Continue! Waiting for navigation...")
                    try:
                        await page.wait_for_url(lambda url: "/auth/" not in url, timeout=15000)
                        print(f"    Navigated to: {page.url}")
                    except:
                        print(f"    URL after wait: {page.url}")
                    await page.wait_for_timeout(5000)
                else:
                    # Nuclear option: force-call React onClick on Continue button
                    print("    Force-calling React onClick on Continue...")
                    await page.evaluate("""(() => {
                        const btns = Array.from(document.querySelectorAll('button'));
                        const btn = btns.find(b => b.textContent.trim() === 'Continue');
                        if (btn) {
                            btn.disabled = false;
                            const propsKey = Object.keys(btn).find(k => k.startsWith('__reactProps$'));
                            if (propsKey && btn[propsKey].onClick) {
                                btn[propsKey].onClick({preventDefault: ()=>{}, stopPropagation: ()=>{}});
                            }
                            btn.click();
                        }
                    })()""")
                    await page.wait_for_timeout(5000)

        # Check if we need to handle anything else after Continue
        await page.wait_for_timeout(3000)
        post_text = await page.evaluate("document.body.innerText.substring(0, 500)")
        print(f"[7b] Page text after Continue: {post_text[:200]}")

        # Final state
        current_url = page.url
        print(f"\n[8] Final URL: {current_url}")
        await page.screenshot(path=r"C:\Agent Carol\data\togal_final_state.png")

        if "/auth/" in current_url:
            # EUSA might have been accepted — try login again
            print("[8b] EUSA accepted but still at login. Trying login again...")
            await page.fill('#email', auth["email"])
            await page.fill('#password', auth["password"])
            await page.wait_for_timeout(500)
            await page.click('#login')
            try:
                await page.wait_for_url(lambda url: "/auth/" not in url, timeout=15000)
                print(f"[8c] Navigated to: {page.url}")
                current_url = page.url
            except:
                # Try navigating directly
                print("[8c] Still at login. Trying direct navigation...")
                project_url = f"https://app.togal.ai/project/{project_id}"
                await page.goto(project_url, wait_until="networkidle", timeout=15000)
                await page.wait_for_timeout(3000)
                current_url = page.url
                print(f"[8d] After direct nav: {current_url}")

        if "/auth/" not in current_url:
            print("[SUCCESS] Logged in!")
            project_url = f"https://app.togal.ai/project/{project_id}?planSetId={set_id}"
            await page.goto(project_url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)
            print(f"[9] At project: {page.url}")
            await page.screenshot(path=r"C:\Agent Carol\data\togal_project.png")

            # Upload file
            file_input = page.locator('input[type="file"]')
            if await file_input.count() == 0:
                # Click upload button first
                upload = page.locator('button:has-text("Upload"), [class*="upload" i]')
                if await upload.count() > 0:
                    await upload.first.click()
                    await page.wait_for_timeout(2000)

            if await file_input.count() > 0:
                print(f"[10] Uploading {DRAWING_PATH.name}...")
                await file_input.first.set_input_files(str(DRAWING_PATH))
                for t in range(30):
                    await page.wait_for_timeout(10000)
                    print(f"    Tick {t+1}/30")
                await page.screenshot(path=r"C:\Agent Carol\data\togal_upload_done.png")
                print("[11] Upload complete")
            else:
                print("[10] No file input found")
                await page.screenshot(path=r"C:\Agent Carol\data\togal_no_upload.png")
        else:
            print("[BLOCKED] Still on auth page")
            # Save page state for debugging
            html = await page.content()
            Path(r"C:\Agent Carol\data\togal_blocked_final.html").write_text(html, encoding='utf-8')

        await page.wait_for_timeout(3000)
        await browser.close()
        print("[DONE]")

if __name__ == "__main__":
    asyncio.run(main())
