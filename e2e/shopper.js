// A shopper's walk through the chat page at phone width, against scripts/e2e_server.py
// (the real app and live store, scripted model). Prints a check per step and saves
// screenshots to the directory given.
//
//   node e2e/shopper.js "$(npm root -g)" docs/uat/e2e
const path = require("path");
const fs = require("fs");
const { chromium } = require(path.join(process.argv[2], "playwright"));
const out = process.argv[3] || ".";
fs.mkdirSync(out, { recursive: true });

(async () => {
  const browser = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome" });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 1 });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  const checks = [];
  const check = (name, ok, detail) => { checks.push({ name, ok: !!ok, detail }); console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${detail ? " - " + detail : ""}`); };
  const settle = async () => {
    await page.waitForSelector(".status", { state: "attached", timeout: 10000 }).catch(() => {});
    await page.waitForSelector(".status", { state: "detached", timeout: 120000 });
    await page.waitForTimeout(250);
  };
  const say = async (text) => { await page.fill("#input", text); await page.click("#send"); await settle(); };
  const shot = (name) => page.screenshot({ path: path.join(out, name + ".png"), fullPage: true });
  const noOverflow = async () => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);

  await page.goto("http://localhost:8000/");
  await page.waitForSelector(".chip", { timeout: 15000 });
  const starters = await page.$$eval(".chip", (n) => n.map((c) => c.textContent));
  check("greeting chips are the store's collections", starters.length >= 3 && starters.every((c) => c.startsWith("Shop ")), starters.join(" / "));

  // 1. Discovery from a chip, then Add to cart on a card.
  await page.locator(".chip").first().click(); await settle();
  const cards = await page.$$eval(".product .name", (n) => n.map((x) => x.textContent));
  check("chip -> product cards", cards.length >= 2, cards.slice(0, 3).join(" / "));
  await page.locator(".product button", { hasText: /^Add to cart$/ }).first().click();
  await page.waitForSelector("#checkout-bar:not([hidden])", { timeout: 30000 });
  check("Add to cart from a card -> checkout bar", true, await page.textContent("#checkout-btn"));
  await shot("1-discovery");

  // 2. Product with options: choose a size on the card.
  await say("What sizes does the logo tee come in?");
  await page.locator(".product button", { hasText: /^Choose( options)?$/ }).last().click();
  await page.waitForSelector(".variants button", { timeout: 30000 });
  const sizes = await page.$$eval(".variants button", (n) => n.map((b) => b.textContent));
  await page.locator(".variants button:not([disabled])", { hasText: "M" }).last().click();
  await page.waitForTimeout(1500);
  check("choose a size on the card -> added", (await page.textContent("#checkout-btn")).includes("2 items"), `sizes ${sizes.join(",")}`);

  // 3. In-chat product page.
  await page.locator(".product .view", { hasText: "View details" }).first().click();
  await page.waitForSelector("#sheet:not([hidden]) .sheet-title", { timeout: 30000 });
  check("View details opens the product page in the chat", page.url().startsWith("http://localhost:8000/"), await page.textContent(".sheet-title"));
  await shot("2-product-page");
  await page.click(".sheet-back");

  // 4. Comparison across countries, with the facts table.
  await say("compare japan and malaysia trips");
  const rows = await page.$$eval("table.matrix tbody tr th", (n) => n.map((x) => x.textContent));
  check("comparison card shows pros and cons", (await page.$$(".compare ul.pc")).length >= 2);
  check("comparison card with a facts table", rows.includes("Duration") && rows.includes("Price"), rows.join(", "));
  const cells = await page.$$eval("table.matrix", (t) => [...t[t.length - 1].querySelectorAll("td")].map((c) => c.textContent.trim()));
  check("every cell of the table has a value", cells.length >= 4 && cells.every((c) => c.length > 0), `${cells.length} cells`);
  check("a sentence comes with the comparison card", /less than|differ/.test(await page.$$eval(".msg.bot", (n) => n[n.length - 1].textContent)));
  await shot("3-compare");

  // 4b. "Compare these two": the products the last reply showed, not a new search.
  await say("Gift ideas for my sister");
  const shownNow = await page.$$eval(".product .name", (n) => n.slice(-3).map((x) => x.textContent));
  await say("compare these two");
  const heads = await page.$$eval("table.matrix", (t) => [...t[t.length - 1].querySelectorAll("thead th")].map((c) => c.textContent).filter(Boolean));
  check("'compare these two' compares the products just shown", heads.length === 2 && heads.every((h) => shownNow.includes(h)), heads.join(" vs "));

  // 5. Plan table (telecom).
  await say("compare your mobile plans");
  const plan = await page.$$eval("table.matrix", (t) => t.length);
  check("plan table", plan >= 2);

  // 6. Itinerary (travel).
  await say("plan a 2-day trip to Singapore");
  check("itinerary card with products", (await page.$$eval(".panel .product", (n) => n.length)) >= 2);
  await shot("4-itinerary");

  // 7. Policy and handoff.
  await say("How many days do I have to return an item?");
  const last = async () => page.$$eval(".msg.bot", (n) => n[n.length - 1].textContent);
  check("return policy from the store's FAQ", (await last()).includes("30 days"));
  await say("I want to talk to a human");
  const mail = await page.$$eval(".msg.bot a[href^='mailto:']", (n) => n.map((a) => a.getAttribute("href")));
  check("handoff email is a mailto link", mail.includes("mailto:tanyueting96@gmail.com"));

  // 8. Checkout.
  await page.click("#checkout-btn");
  await page.waitForSelector("#checkout-go:not([hidden])", { timeout: 30000 });
  const href = await page.getAttribute("#checkout-go", "href");
  check("checkout bar -> Shopify checkout link", /^https:\/\/[\w.-]+\.myshopify\.com\/(cart|checkouts)\//.test(href), href.slice(0, 60));
  await shot("5-checkout");

  // 9. A dropped connection mid-reply: a plain message and Try again, not the browser's.
  await page.route("**/api/chat", (route) => route.abort("connectionreset"), { times: 1 });
  await page.fill("#input", "Any gift for my dad?"); await page.click("#send");
  await page.waitForSelector("#log .error", { timeout: 20000 });
  const dropped = await page.$$eval("#log .error", (n) => n[n.length - 1].textContent);
  check("dropped connection -> friendly message with Try again", /connection dropped/i.test(dropped) && /Try again/.test(dropped), dropped.slice(0, 70));
  const before = (await page.$$(".product")).length;
  await page.locator("#log .error button", { hasText: "Try again" }).last().click(); await settle();
  check("Try again resends and gets the reply", (await page.$$(".product")).length > before && (await page.$$("#log .error")).length === 0);

  // 10. The model is unavailable (out of credit): still cards, add, checkout.
  await page.evaluate(() => fetch("/e2e/outage/1", { method: "POST" }));
  await say("I need a travel adapter");
  const fallbackText = await last();
  const fallbackCards = await page.$$eval(".product .name", (n) => n.map((x) => x.textContent));
  check("model outage -> products from the store, no dead end", /unavailable right now/.test(fallbackText) && fallbackCards.some((t) => /adapter/i.test(t)), fallbackText.slice(0, 70));
  await page.locator(".product", { hasText: /Adapter/ }).last().locator("button", { hasText: /^Add to cart$/ }).click();
  await page.waitForTimeout(1500);
  check("model outage -> Add to cart still works", (await page.textContent("#checkout-btn") || "").includes("3 items") || !(await page.isHidden("#checkout-go")), await page.textContent("#checkout-btn"));
  await say("What is your return policy?");
  check("model outage -> policy answered from the FAQ", /30 days/.test(await last()));
  await page.evaluate(() => fetch("/e2e/outage/0", { method: "POST" }));
  await shot("6-outage");

  check("no raw JSON on the page", (await page.$$("pre")).length === 0);
  check("no sideways scroll at phone width", await noOverflow());
  check("no page errors", errors.length === 0, errors.join(" | "));
  await browser.close();
  const failed = checks.filter((c) => !c.ok).length;
  console.log(`\n${checks.length - failed}/${checks.length} checks passed`);
  process.exit(failed ? 1 : 0);
})();
