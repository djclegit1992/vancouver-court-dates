// The staleness banner is the one feature you cannot see working in
// normal operation, so it gets its own harness. Four scenarios, each
// with the clock and the payload set deliberately.

const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');
const html = fs.readFileSync('vcd-block.html', 'utf8');
const base = JSON.parse(fs.readFileSync('sample_latest.json', 'utf8'));

let pass = 0, fail = 0;
function check(label, got, want) {
  if (got === want) { console.log('  ok    ' + label); pass++; }
  else { console.log('  FAIL  ' + label + ': got ' + JSON.stringify(got) + ', want ' + JSON.stringify(want)); fail++; }
}

// Court's newest build in the fixture, as Pacific.
const built = base.documents.map(x => x.date_created).filter(Boolean).sort().pop();
const builtMs = Date.parse(built + ':00-07:00');

function scenario(name, nowIso, checkedMs, expectStale, expectPhrase) {
  const payload = JSON.parse(JSON.stringify(base));
  payload.generated_at = new Date(checkedMs).toISOString();
  const vc = new VirtualConsole();
  const dom = new JSDOM('<!DOCTYPE html><body>' + html + '</body>', {
    runScripts: 'dangerously',
    virtualConsole: vc,
    beforeParse(w) {
      const FIXED = Date.parse(nowIso);
      const R = w.Date;
      function F(...a) { return a.length ? new R(...a) : new R(FIXED); }
      F.prototype = R.prototype; F.now = () => FIXED; F.parse = R.parse; F.UTC = R.UTC;
      w.Date = F;
      w.XMLHttpRequest = function () {
        this.open = function () {}; this.setRequestHeader = function () {};
        this.send = function () {
          this.readyState = 4; this.status = 200;
          this.responseText = JSON.stringify(payload);
          if (this.onreadystatechange) { this.onreadystatechange(); }
        };
      };
      w.setInterval = function () { return 0; };
    }
  });
  return new Promise(res => setTimeout(() => {
    const d = dom.window.document;
    const warn = d.querySelector('#vcdWarn');
    const dot = d.querySelector('#vcdDot');
    const on = warn.className.indexOf('vcd-warn-on') !== -1;
    console.log('\n' + name);
    check('  banner shown', on, expectStale);
    check('  dot amber', dot.className.indexOf('vcd-dot-stale') !== -1, expectStale);
    if (expectPhrase) {
      check('  explains why', new RegExp(expectPhrase).test(warn.textContent), true);
    }
    res();
  }, 250));
}

(async () => {
  // Fixture's newest court build is 2026-07-27 17:03 PT = 00:03Z.
  // All scenario clocks are set relative to that, deliberately.

  // 1. Court open, checked 17 minutes ago, after the build. Fresh.
  await scenario('court open, checked 17 minutes ago',
    '2026-07-28T00:35:00Z', Date.parse('2026-07-28T00:18:00Z'), false);

  // 2. Court open, last check 4 hours ago. Stale on elapsed time.
  await scenario('court open, four hours since our last check',
    '2026-07-28T20:00:00Z', Date.parse('2026-07-28T16:00:00Z'), true,
    'we normally check every hour');

  // 3. 02:00 Pacific, court closed. Our check is old in clock terms but
  //    came after the court's last build, so nothing has been missed.
  //    This is the nightly false alarm a flat time rule would produce.
  await scenario('2am Pacific, court closed, nothing missed',
    '2026-07-28T09:00:00Z', Date.parse('2026-07-28T00:18:00Z'), false);

  // 4. Same hour, but our last check predates the court's last build.
  //    We are genuinely a cycle behind, so warn.
  await scenario('2am Pacific, but we missed the last rebuild',
    '2026-07-28T09:00:00Z', Date.parse('2026-07-27T23:00:00Z'), true,
    'rebuilt its lists since our last successful check');

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})();
