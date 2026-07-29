// Test the confirmation Edge Function without deploying it.
//
// Mocks Deno.env, Deno.serve, and every outbound fetch, then drives the
// handler through the paths that decide whether a stranger can make
// your Postmark account send mail.
//
// Usage:
//   npx tsc --project tsconfig.test.json     (emits build/index.js)
//   node test_edge.mjs

import { readFileSync } from "node:fs";

const SECRET = "test-shared-secret-value-1234567890";

let pass = 0, fail = 0;
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (ok) { console.log("  ok    " + label); pass++; }
  else { console.log("  FAIL  " + label + ": got " + JSON.stringify(got) + ", want " + JSON.stringify(want)); fail++; }
}
function checkTrue(label, got) { check(label, !!got, true); }

// ---- the world the function runs in ------------------------------
const world = {
  postmarkCalls: [],
  patchCalls: [],
  postmarkStatus: 200,
  patchStatus: 204,
  latestStatus: 200,
  latest: {
    documents: [
      { slug: "3-day-civil-trials", dates: ["2027-03-22", "2027-03-29"] },
      { slug: "civil-lengthy-chambers-available-dates", dates: [] },
    ],
  },
};

globalThis.Deno = {
  env: {
    get(k) {
      return {
        WEBHOOK_SECRET_BC: SECRET,
        POSTMARK_TOKEN_BC: "fake-token",
        POSTMARK_FROM_BC: "Courtready <alerts@courtready.ca>",
        POSTMARK_STREAM_BC: "outbound",
        // The Winnipeg names, deliberately set to values that would
        // break things if the function read them by mistake.
        WEBHOOK_SECRET: "winnipeg-secret-do-not-use",
        POSTMARK_TOKEN: "winnipeg-token-do-not-use",
        POSTMARK_FROM: "Courtready <winnipeg@courtready.ca>",
        POSTMARK_STREAM: "winnipeg-stream",
        SUPABASE_URL: "https://fake.supabase.co",
        SUPABASE_SERVICE_ROLE_KEY: "fake-service-key",
      }[k];
    },
  },
  serve(h) { globalThis.__handler = h; },
};

globalThis.fetch = async (url, opts = {}) => {
  const u = String(url);
  if (u.includes("raw.githubusercontent.com")) {
    if (world.latestStatus !== 200) {
      return { ok: false, status: world.latestStatus, json: async () => ({}) };
    }
    return { ok: true, status: 200, json: async () => world.latest };
  }
  if (u.includes("postmarkapp.com")) {
    if (world.postmarkStatus !== 200) {
      return { ok: false, status: world.postmarkStatus,
               text: async () => "simulated postmark failure" };
    }
    world.postmarkCalls.push(JSON.parse(opts.body));
    return { ok: true, status: 200, text: async () => "{}" };
  }
  if (u.includes("/rest/v1/court_alerts")) {
    world.patchCalls.push({ url: u, body: JSON.parse(opts.body) });
    return { ok: world.patchStatus < 300, status: world.patchStatus,
             text: async () => "" };
  }
  throw new Error("unexpected fetch: " + u);
};

await import("./build/send-confirmation-bc/index.js");
const handler = globalThis.__handler;

function req(body, { secret = SECRET, method = "POST" } = {}) {
  return new Request("https://fn.example/send-confirmation-bc", {
    method,
    headers: { "x-webhook-secret": secret, "Content-Type": "application/json" },
    // wrapped as the trigger sends it
    body: method === "POST" ? JSON.stringify({ record: body }) : undefined,
  });
}

function reset() {
  world.postmarkCalls = [];
  world.patchCalls = [];
  world.postmarkStatus = 200;
  world.patchStatus = 204;
  world.latestStatus = 200;
}

const VALID = {
  id: 42,
  email: "someone@example.com",
  hearing_code: "3-day-civil-trials",
  hearing_name: "3 Day Civil Trials",
  jurisdiction: "BC",
  location_code: "VA",
  wanted_by: null,
  confirmation_sent_at: null,
};

// ---- who is allowed to make this send mail -----------------------
console.log("authorisation");
reset();
let r = await handler(req(VALID, { secret: "wrong" }));
check("wrong secret rejected", r.status, 401);
check("nothing sent", world.postmarkCalls.length, 0);

reset();
r = await handler(req(VALID, { secret: "" }));
check("empty secret rejected", r.status, 401);

reset();
r = await handler(req(VALID, { secret: SECRET + "x" }));
check("longer secret rejected", r.status, 401);

// The Winnipeg secrets exist in this project. The function must not
// read them, or the two tools cross wires.
reset();
r = await handler(req(VALID, { secret: "winnipeg-secret-do-not-use" }));
check("Winnipeg shared secret rejected", r.status, 401);
reset();
r = await handler(req(VALID));
check("uses the BC stream, not Winnipeg's",
  world.postmarkCalls[0].MessageStream, "outbound");
check("sends from the BC address, not Winnipeg's",
  world.postmarkCalls[0].From, "Courtready <alerts@courtready.ca>");

reset();
r = await handler(req(VALID, { method: "GET" }));
check("GET rejected", r.status, 405);

// ---- input it should refuse --------------------------------------
console.log("\nboth body shapes");
reset();
r = await handler(new Request("https://fn.example/x", {
  method: "POST",
  headers: { "x-webhook-secret": SECRET, "Content-Type": "application/json" },
  body: JSON.stringify(VALID),
}));
check("a bare row is accepted", r.status, 200);
check("and sends", world.postmarkCalls.length, 1);

reset();
r = await handler(new Request("https://fn.example/x", {
  method: "POST",
  headers: { "x-webhook-secret": SECRET, "Content-Type": "application/json" },
  body: JSON.stringify({ record: VALID }),
}));
check("a trigger-wrapped row is accepted", r.status, 200);

reset();
r = await handler(new Request("https://fn.example/x", {
  method: "POST",
  headers: { "x-edge-secret": SECRET, "Content-Type": "application/json" },
  body: JSON.stringify({ record: VALID }),
}));
check("the old header name no longer works", r.status, 401);

console.log("\ninput validation");
reset();
r = await handler(new Request("https://fn.example/x", {
  method: "POST", headers: { "x-webhook-secret": SECRET }, body: "not json",
}));
check("bad json rejected", r.status, 400);

reset();
r = await handler(req({ id: 1, email: "a@b.co" }));
check("missing hearing_code rejected", r.status, 400);

reset();
r = await handler(req({ ...VALID, jurisdiction: "MB" }));
check("wrong jurisdiction rejected", r.status, 400);
check("no email sent to a Winnipeg row", world.postmarkCalls.length, 0);

reset();
r = await handler(req({ ...VALID, location_code: "01  " }));
check("wrong location rejected", r.status, 400);

// ---- the case that would double-send -----------------------------
console.log("\nalready confirmed");
reset();
r = await handler(req({ ...VALID, confirmation_sent_at: "2026-07-28T10:00:00Z" }));
check("returns ok", r.status, 200);
check("but sends nothing", world.postmarkCalls.length, 0);
check("and marks nothing", world.patchCalls.length, 0);

// ---- the happy path ----------------------------------------------
console.log("\nvalid signup, no threshold");
reset();
r = await handler(req(VALID));
check("status", r.status, 200);
check("one email", world.postmarkCalls.length, 1);
const mail = world.postmarkCalls[0];
check("to the subscriber", mail.To, "someone@example.com");
check("subject", mail.Subject, "Alert confirmed: 3 Day Civil Trials");
checkTrue("has an HTML body", !!mail.HtmlBody);
checkTrue("html names the list", mail.HtmlBody.includes("3 Day Civil Trials"));
checkTrue("html links the tool as anchor text, not a bare url",
  /<a href="https:\/\/courtready\.ca\/vancouver-court-dates-finder\/"[^>]*>Courtready/.test(mail.HtmlBody));
checkTrue("html offers a way out",
  /reply to this email/.test(mail.HtmlBody));
checkTrue("html has the independence line",
  /independent organisation/.test(mail.HtmlBody));
check("no em dash in the html", /\u2014/.test(mail.HtmlBody), false);
check("no em dash in the text", /\u2014/.test(mail.TextBody), false);
checkTrue("says any date", /offers any date/.test(mail.TextBody));
checkTrue("quotes the current earliest", mail.TextBody.includes("2027-03-22"));
checkTrue("mentions hourly checking", /every hour/.test(mail.TextBody));
checkTrue("phone number present", mail.TextBody.includes("604.660.2853"));
checkTrue("independence line", /independent organisation/.test(mail.TextBody));
check("uses the right stream", mail.MessageStream, "outbound");
check("row marked", world.patchCalls.length, 1);
checkTrue("marks the right row", world.patchCalls[0].url.includes("id=eq.42"));
checkTrue("sets confirmation_sent_at",
  !!world.patchCalls[0].body.confirmation_sent_at);

// ---- with a threshold ---------------------------------------------
console.log("\nvalid signup, with a threshold");
reset();
r = await handler(req({ ...VALID, wanted_by: "2027-06-01" }));
checkTrue("quotes the threshold back in text",
  world.postmarkCalls[0].TextBody.includes("on or before 2027-06-01"));
checkTrue("quotes the threshold back in html",
  world.postmarkCalls[0].HtmlBody.includes("2027-06-01"));

// ---- an empty list -------------------------------------------------
console.log("\nsignup to a list with no dates");
reset();
r = await handler(req({
  ...VALID,
  hearing_code: "civil-lengthy-chambers-available-dates",
  hearing_name: "Civil Lengthy Chambers Available Dates",
}));
checkTrue("says the list is empty, not a false date",
  /no dates on it at all/.test(world.postmarkCalls[0].TextBody));
checkTrue("html says so too",
  /no dates on it at all/.test(world.postmarkCalls[0].HtmlBody));

// ---- degraded but not broken ---------------------------------------
console.log("\nlatest.json unreachable");
reset();
world.latestStatus = 500;
r = await handler(req(VALID));
check("still sends", r.status, 200);
check("one email", world.postmarkCalls.length, 1);
checkTrue("omits the current-dates line rather than guessing",
  !/earliest date on that list/.test(world.postmarkCalls[0].TextBody));

// ---- failure paths --------------------------------------------------
console.log("\nhtml escaping");
reset();
r = await handler(req({ ...VALID, hearing_name: 'Trials <b>& "Chambers"</b>' }));
checkTrue("angle brackets escaped",
  !/<b>/.test(world.postmarkCalls[0].HtmlBody));
checkTrue("ampersand escaped",
  /&amp;/.test(world.postmarkCalls[0].HtmlBody));

console.log("\nPostmark refuses");
reset();
world.postmarkStatus = 422;
r = await handler(req(VALID));
check("reports failure so the backstop retries", r.status, 500);
check("row NOT marked confirmed", world.patchCalls.length, 0);

console.log("\nsent but the database update fails");
reset();
world.patchStatus = 500;
r = await handler(req(VALID));
check("email did go", world.postmarkCalls.length, 1);
check("returns 200 with a warning, not a silent success", r.status, 200);
const payload = JSON.parse(await r.text());
checkTrue("warning present", !!payload.warning);

console.log("\n" + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
