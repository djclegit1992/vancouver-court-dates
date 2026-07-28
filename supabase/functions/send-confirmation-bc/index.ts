// Courtready Vancouver Court Dates Finder
// Confirmation email.
//
// Called two ways, both of which arrive here rather than composing
// their own copy:
//
//   1. A Postgres AFTER INSERT trigger, immediately on signup.
//   2. The backstop sweep in alerts.py, for signups whose trigger
//      never fired. A trigger fires once and does not retry, so if
//      Postmark blips at the wrong moment someone signs up and hears
//      nothing at all.
//
// This is the ONLY place the confirmation email is written. The
// alternative was composing it here and again in Python, which is the
// same email in two languages, drifting apart the first time anyone
// rewords it.
//
// Called with the Winnipeg convention: header 'x-webhook-secret' and a
// body of {"record": {...}}. A bare row object is accepted too.
//
// Deploy:
//   supabase functions deploy send-confirmation-bc --no-verify-jwt
//
// Secrets required:
//   WEBHOOK_SECRET_BC   any long random string you invent
//   POSTMARK_TOKEN_BC   the Vancouver server token
//   POSTMARK_FROM_BC    a verified sender on THAT server
//   POSTMARK_STREAM_BC  optional, defaults to 'outbound'
//
// All three are suffixed because secrets are shared across the whole
// Supabase project, and the Winnipeg tool already owns the plain names.
//
// --no-verify-jwt is deliberate: the caller is a database trigger, not
// a signed-in user. The shared secret is what stands between this URL
// and anyone who finds it, so it is checked before anything else runs.

// Suffixed, deliberately. Edge Function secrets are per PROJECT, not
// per function, and this project already runs the Winnipeg tool.
// Reusing POSTMARK_TOKEN would repoint Winnipeg confirmations at the
// Vancouver server, and Postmark suppressions are per stream: someone
// who unsubscribed from one tool would silently stop receiving the
// other. That is the exact failure a separate server exists to avoid.
const SHARED_SECRET = Deno.env.get("WEBHOOK_SECRET_BC") ?? "";
const POSTMARK_TOKEN = Deno.env.get("POSTMARK_TOKEN_BC") ?? "";
const POSTMARK_STREAM = Deno.env.get("POSTMARK_STREAM_BC") ?? "outbound";
const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

const FROM = Deno.env.get("POSTMARK_FROM_BC") ?? "";
const REPLY_TO = "admin@courtready.ca";
const TOOL_URL = "https://courtready.ca/vancouver-court-dates-finder/";
const PHONE = "604.660.2853";
const DATA_URL =
  "https://raw.githubusercontent.com/djclegit1992/vancouver-court-dates/main/data/latest.json";

const JURISDICTION = "BC";
const LOCATION_CODE = "VA";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// Constant-time-ish comparison. Not critical here, but a plain !==
// leaks length and a little timing, and it costs nothing to avoid.
function secretMatches(given: string): boolean {
  if (!SHARED_SECRET) return false;
  if (given.length !== SHARED_SECRET.length) return false;
  let diff = 0;
  for (let i = 0; i < given.length; i++) {
    diff |= given.charCodeAt(i) ^ SHARED_SECRET.charCodeAt(i);
  }
  return diff === 0;
}

type Row = {
  id: number;
  email: string;
  hearing_code: string;
  hearing_name?: string | null;
  wanted_by?: string | null;
  jurisdiction?: string | null;
  location_code?: string | null;
  confirmation_sent_at?: string | null;
};

// The current picture, so the confirmation can say what the list looks
// like today. Best effort: if this fails the email still goes, just
// without that line. Never let a nice-to-have block the confirmation.
async function currentDates(slug: string): Promise<string[] | null> {
  try {
    const r = await fetch(DATA_URL + "?t=" + Date.now(), {
      signal: AbortSignal.timeout(8000),
    });
    if (!r.ok) return null;
    const data = await r.json();
    const docs = data.documents ?? [];
    for (const d of docs) {
      if (d.slug === slug) return d.dates ?? [];
    }
    return null;
  } catch (_e) {
    return null;
  }
}

function body(row: Row, dates: string[] | null): string {
  const name = row.hearing_name || row.hearing_code;
  const lines: string[] = [];

  if (row.wanted_by) {
    lines.push(
      `We'll email you once, when the Supreme Court next offers a date for ${name} in Vancouver on or before ${row.wanted_by}.`,
    );
  } else {
    lines.push(
      `We'll email you once, when the Supreme Court next offers any date for ${name} in Vancouver.`,
    );
  }
  lines.push("");

  if (dates !== null) {
    if (dates.length === 0) {
      lines.push("Right now that list has no dates on it at all.");
    } else {
      lines.push(`Right now the earliest date on that list is ${dates[0]}.`);
    }
  }
  lines.push("We check every hour through the court's working day.");
  lines.push("");
  lines.push(`To book, call Supreme Court Scheduling on ${PHONE}.`);
  lines.push("");
  lines.push(`See the full list: ${TOOL_URL}`);
  lines.push("");
  lines.push(
    "Courtready.ca is an independent organisation and is not affiliated with the court.",
  );
  return lines.join("\n");
}

async function send(row: Row, dates: string[] | null) {
  const name = row.hearing_name || row.hearing_code;
  const r = await fetch("https://api.postmarkapp.com/email", {
    method: "POST",
    headers: {
      "X-Postmark-Server-Token": POSTMARK_TOKEN,
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify({
      From: FROM,
      To: row.email,
      ReplyTo: REPLY_TO,
      Subject: `You're on the list for ${name}`,
      TextBody: body(row, dates),
      MessageStream: POSTMARK_STREAM,
    }),
    signal: AbortSignal.timeout(15000),
  });
  if (!r.ok) {
    const detail = await r.text();
    throw new Error(`postmark ${r.status}: ${detail.slice(0, 200)}`);
  }
}

async function markSent(id: number) {
  const r = await fetch(
    `${SUPABASE_URL}/rest/v1/court_alerts?id=eq.${id}`,
    {
      method: "PATCH",
      headers: {
        apikey: SERVICE_KEY,
        Authorization: `Bearer ${SERVICE_KEY}`,
        "Content-Type": "application/json",
        Prefer: "return=minimal",
      },
      body: JSON.stringify({ confirmation_sent_at: new Date().toISOString() }),
      signal: AbortSignal.timeout(10000),
    },
  );
  if (!r.ok) {
    throw new Error(`could not mark row ${id}: ${r.status}`);
  }
}

Deno.serve(async (req: Request) => {
  if (req.method !== "POST") return json(405, { error: "POST only" });

  // Secret first, before parsing anything. This URL is public.
  // Header name matches the Winnipeg trigger's convention.
  if (!secretMatches(req.headers.get("x-webhook-secret") ?? "")) {
    return json(401, { error: "unauthorized" });
  }
  if (!POSTMARK_TOKEN) {
    return json(500, { error: "POSTMARK_TOKEN_BC not set" });
  }
  // A From address that is not a verified sender on this server
  // makes Postmark reject every single send. Fail loudly here
  // rather than silently bouncing every confirmation.
  if (!FROM) {
    return json(500, { error: "POSTMARK_FROM_BC not set" });
  }

  // The Postgres trigger sends {"record": {...}}, matching the
  // Winnipeg convention. The backstop in alerts.py sends the same
  // shape. A bare row is accepted too, so a hand-run curl works.
  let row: Row;
  try {
    const parsed = await req.json();
    row = (parsed && parsed.record) ? parsed.record : parsed;
  } catch (_e) {
    return json(400, { error: "bad json" });
  }

  if (!row || !row.id || !row.email || !row.hearing_code) {
    return json(400, { error: "need id, email and hearing_code" });
  }

  // Only ever act on this tool's rows. The Winnipeg trigger has its own
  // function, and crossing them would send the wrong copy from the
  // wrong Postmark stream.
  if (row.jurisdiction && row.jurisdiction !== JURISDICTION) {
    return json(400, { error: "wrong jurisdiction" });
  }
  if (row.location_code && row.location_code !== LOCATION_CODE) {
    return json(400, { error: "wrong location" });
  }

  // Already confirmed. The backstop and the trigger can both reach the
  // same row if a run overlaps a signup, and nobody should get two.
  if (row.confirmation_sent_at) {
    return json(200, { ok: true, skipped: "already confirmed" });
  }

  try {
    const dates = await currentDates(row.hearing_code);
    await send(row, dates);
  } catch (e) {
    console.error("send failed", e);
    // 500 so the caller knows. The backstop in alerts.py will retry
    // this row within the hour.
    return json(500, { error: String(e) });
  }

  try {
    await markSent(row.id);
  } catch (e) {
    // Sent but not marked. Say so loudly rather than pretending.
    // Worst case the backstop sends a second confirmation, which is
    // noise rather than harm.
    console.error("sent but not marked", e);
    return json(200, { ok: true, warning: String(e) });
  }

  return json(200, { ok: true });
});
