// Render the block headlessly against a real payload and assert what
// the user actually sees. The block validator proves the file is legal
// WordPress; this proves it is correct.

const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');

const html = fs.readFileSync('vcd-block.html', 'utf8');
const payload = fs.readFileSync('sample_latest.json', 'utf8');

let pass = 0, fail = 0;
function check(label, got, want) {
  if (got === want) { console.log('  ok    ' + label); pass++; }
  else { console.log('  FAIL  ' + label + ': got ' + JSON.stringify(got) + ', want ' + JSON.stringify(want)); fail++; }
}
function checkTrue(label, got) { check(label, !!got, true); }

const vc = new VirtualConsole();
vc.on('jsdomError', e => { console.log('  FAIL  jsdom error: ' + e.message); fail++; });

const dom = new JSDOM('<!DOCTYPE html><body>' + html + '</body>', {
  runScripts: 'dangerously',
  virtualConsole: vc,
  beforeParse(w) {
    // Freeze time so relative text is deterministic.
    const FIXED = new Date('2026-07-28T16:35:00Z').getTime();
    const RealDate = w.Date;
    function FakeDate(...a) {
      if (a.length === 0) { return new RealDate(FIXED); }
      return new RealDate(...a);
    }
    FakeDate.prototype = RealDate.prototype;
    FakeDate.now = () => FIXED;
    FakeDate.parse = RealDate.parse;
    FakeDate.UTC = RealDate.UTC;
    w.Date = FakeDate;

    // Captures POSTs so the signup box can be tested without a
    // database. GETs return the fixture payload as before.
    w.__posted = [];
    w.XMLHttpRequest = function () {
      var self = this;
      this.readyState = 0;
      this.status = 0;
      this.responseText = '';
      this.__method = 'GET';
      this.__url = '';
      this.open = function (m, u) { self.__method = m; self.__url = u; };
      this.send = function (b) {
        self.readyState = 4;
        if (self.__method === 'POST') {
          w.__posted.push({ url: self.__url, body: b });
          self.status = 201;
          self.responseText = '';
        } else {
          self.status = 200;
          self.responseText = payload;
        }
        if (self.onreadystatechange) { self.onreadystatechange(); }
      };
      this.setRequestHeader = function () {};
    };
    w.setInterval = function () { return 0; };
  }
});

const d = dom.window.document;
const posted = dom.window.__posted;
const text = n => (n ? n.textContent.replace(/\s+/g, ' ').trim() : null);

setTimeout(() => {
  console.log('one unified list');
  const cards = d.querySelectorAll('#vcdList .vcd-card');
  check('all 28 availability lists as cards', cards.length, 28);
  check('no separate grid section', d.querySelector('#vcdGrid'), null);

  const titles = Array.from(cards).map(c => text(c.querySelector('.vcd-name')));
  check('no booking guide listed as a hearing type',
    titles.filter(t => /^Booking /.test(t)).length, 0);
  checkTrue('trial lists are present', titles.indexOf('2 Day Civil Trials') !== -1);
  checkTrue('registrar lists are present', titles.indexOf('Bankruptcy Discharge') !== -1);

  console.log('\ncategory tiles');
  const cats = d.querySelectorAll('#vcdCats .vcd-cat');
  const catNames = Array.from(cats).map(c => text(c.querySelector('.vcd-cat-name')));
  check('tile order', catNames.join('|'),
    'Everything|Trials|Conferences|Chambers|Registrar|Criminal|None offered');
  check('no How to book tile', catNames.indexOf('How to book'), -1);
  check('everything counts 28', text(cats[0].querySelector('.vcd-cat-n')), '28');
  check('trials counts 15', text(cats[1].querySelector('.vcd-cat-n')), '15');
  check('conferences counts 4', text(cats[2].querySelector('.vcd-cat-n')), '4');
  check('chambers counts 3', text(cats[3].querySelector('.vcd-cat-n')), '3');
  check('registrar counts 4', text(cats[4].querySelector('.vcd-cat-n')), '4');
  check('criminal counts 2', text(cats[5].querySelector('.vcd-cat-n')), '2');

  console.log('\ntrial cards carry matter and length');
  const civ2 = Array.from(cards).find(c => text(c.querySelector('.vcd-name')) === '2 Day Civil Trials');
  const tags = Array.from(civ2.querySelectorAll('.vcd-tag')).map(t => text(t));
  check('tags', tags.join('|'), 'Trials|Civil|2 days');
  check('date is ISO', text(civ2.querySelector('.vcd-big')), '2027-03-22');
  checkTrue('count and weekday shown', /Mon .* 90 dates/.test(text(civ2.querySelector('.vcd-relr'))));

  const mva2 = Array.from(cards).find(c => text(c.querySelector('.vcd-name')) === '2 Day MVA Trials');
  check('mva label reads Motor vehicle',
    Array.from(mva2.querySelectorAll('.vcd-tag')).map(t => text(t)).join('|'),
    'Trials|Motor vehicle|2 days');

  console.log('\nbooking links');
  const links = n => Array.from(n.querySelectorAll('a')).map(a => a.getAttribute('href'));
  checkTrue('trial card offers online booking',
    links(civ2).some(h => h === 'https://justice.gov.bc.ca/scjob/'));
  checkTrue('trial card links the booking guide',
    links(civ2).some(h => /Booking%20Trials\.pdf$/.test(h)));
  checkTrue('trial card links the source document',
    links(civ2).some(h => /2%20Day%20Civil%20Trials\.pdf$/.test(h)));

  const jcc = Array.from(cards).find(c => /Judicial Case Conference/.test(text(c.querySelector('.vcd-name'))));
  checkTrue('JCC is bookable online',
    links(jcc).some(h => h === 'https://justice.gov.bc.ca/scjob/'));

  const chambers = Array.from(cards).find(c => /Civil Lengthy Chambers/.test(text(c.querySelector('.vcd-name'))));
  checkTrue('chambers bookable online',
    links(chambers).some(h => h === 'https://justice.gov.bc.ca/scjob/'));
  checkTrue('chambers links the lengthy chambers guide',
    links(chambers).some(h => /Booking%20Lengthy%20Chambers\.pdf$/.test(h)));

  const assize = Array.from(cards).find(c => /Assize Chambers/.test(text(c.querySelector('.vcd-name'))));
  checkTrue('assize links its own guide',
    links(assize).some(h => /Booking%20Lengthy%20Assize%20Chambers\.pdf$/.test(h)));

  const sca = Array.from(cards).find(c => /SCA/.test(text(c.querySelector('.vcd-name'))));
  check('criminal list is not offered online booking',
    links(sca).filter(h => h === 'https://justice.gov.bc.ca/scjob/').length, 0);
  const bank = Array.from(cards).find(c => /Bankruptcy/.test(text(c.querySelector('.vcd-name'))));
  check('registrar list is not offered online booking',
    links(bank).filter(h => h === 'https://justice.gov.bc.ca/scjob/').length, 0);

  console.log('\nstates');
  const empty = Array.from(cards).find(c => text(c.querySelector('.vcd-name')) === '6-15 Day Civil Trials');
  check('empty list says none offered', text(empty.querySelector('.vcd-none-txt')), 'None offered');
  check('empty list has the none band', empty.className.indexOf('vcd-b-none') !== -1, true);
  const emptyCount = Array.from(cards).filter(c => c.querySelector('.vcd-none-txt')).length;
  check('seven lists are empty', emptyCount, 7);

  const q = Array.from(cards).find(c => text(c.querySelector('.vcd-name')) === '4-5 Day Civil Trials');
  checkTrue('qualifier explained', /4 day hearings only/.test(text(q.querySelector('.vcd-note-line'))));
  checkTrue('qualified dates marked', q.querySelectorAll('.vcd-date-q').length > 0);

  console.log('\nevery date is ISO');
  const bad = [];
  d.querySelectorAll('.vcd-big, .vcd-date').forEach(n => {
    if (!/^\d{4}-\d{2}-\d{2}/.test(text(n))) { bad.push(text(n)); }
  });
  check('no non-ISO date rendered', bad.length, 0);

  console.log('\nstamps');
  // Do not pin to a literal timestamp: the court rebuilds its lists
  // several times a day, so any fixed value rots the moment the
  // fixture is regenerated. Assert the shape, and that the page shows
  // the NEWEST stamp across all documents.
  const stamp = text(d.querySelector('#vcdStampCourt'));
  checkTrue('court stamp is YYYY-MM-DD, h:mmam/pm',
    /^\d{4}-\d{2}-\d{2}, \d{1,2}:\d{2}(am|pm)$/.test(stamp));
  const created = JSON.parse(payload).documents
    .map(x => x.date_created).filter(Boolean).sort();
  const newest = created[created.length - 1];
  check('court stamp is the newest date_created',
    stamp.slice(0, 10), newest.slice(0, 10));
  checkTrue('our stamp is relative', /ago|moments/.test(text(d.querySelector('#vcdStampUs'))));
  check('staleness hidden when fresh', d.querySelector('#vcdWarn').className, 'vcd-warn');
  checkTrue('copy says hourly, not five times a day',
    !/five times a day/.test(d.body.textContent));
  checkTrue('hero mentions hourly',
    /every hour/.test(text(d.querySelector('.vcd-sub'))));
  check('unread key hidden', d.querySelector('#vcdKeyUnread').className, 'vcd-key vcd-key-hide');

  console.log('\nnone offered tile');
  const none = Array.from(cats).find(c => text(c.querySelector('.vcd-cat-name')) === 'None offered');
  checkTrue('tile exists', !!none);
  check('counts the seven empty lists', text(none.querySelector('.vcd-cat-n')), '7');
  checkTrue('tile is dashed', none.className.indexOf('vcd-cat-none') !== -1);
  none.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  const noneCards = d.querySelectorAll('#vcdList .vcd-card');
  check('filters to seven', noneCards.length, 7);
  checkTrue('every card in it says none offered',
    Array.from(noneCards).every(c => !!c.querySelector('.vcd-none-txt')));
  checkTrue('spans more than one group',
    new Set(Array.from(noneCards).map(c => text(c.querySelector('.vcd-tag')))).size > 1);
  const selected = Array.from(d.querySelectorAll('#vcdCats .vcd-cat')).find(c => text(c.querySelector('.vcd-cat-name')) === 'None offered');
  checkTrue('selected state applies', selected.className.indexOf('vcd-cat-on') !== -1);
  d.querySelectorAll('#vcdCats .vcd-cat')[0].dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  check('everything restores after', d.querySelectorAll('#vcdList .vcd-card').length, 28);

  console.log('\ninteraction');
  // Re-query every time: clicking a tile re-renders the tile row, so
  // any handle taken earlier points at a detached node.
  function tile(name) {
    return Array.from(d.querySelectorAll('#vcdCats .vcd-cat'))
      .find(c => text(c.querySelector('.vcd-cat-name')) === name);
  }
  function clickTile(name) {
    tile(name).dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  }
  clickTile('Trials');
  check('trials filter shows 15', d.querySelectorAll('#vcdList .vcd-card').length, 15);
  clickTile('Everything');
  check('everything restores 28', d.querySelectorAll('#vcdList .vcd-card').length, 28);

  const search = d.querySelector('#vcdSearch');
  search.value = '3 day';
  search.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  check('search by length finds three', d.querySelectorAll('#vcdList .vcd-card').length, 3);
  search.value = 'motor vehicle';
  search.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  check('search by matter label finds five', d.querySelectorAll('#vcdList .vcd-card').length, 5);
  search.value = '';
  search.dispatchEvent(new dom.window.Event('input', { bubbles: true }));

  const more = d.querySelector('#vcdList .vcd-more');
  checkTrue('a show-all control exists', !!more);
  if (more) {
    const card = more.closest('.vcd-card');
    check('preview capped at twelve', card.querySelectorAll('.vcd-date').length, 12);
    more.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
    checkTrue('expanding shows more',
      d.querySelector('#vcdList .vcd-card').querySelectorAll('.vcd-date').length > 12);
  }

  // ---- signup box ------------------------------------------------
  console.log('\nsignup box');
  const cardsNow = d.querySelectorAll('#vcdList .vcd-card');
  check('every card has a signup button',
    Array.from(cardsNow).filter(c => c.querySelector('[data-alert]')).length, 28);
  check('boxes start closed',
    d.querySelectorAll('#vcdList .vcd-box-on').length, 0);

  function cardFor(title) {
    return Array.from(d.querySelectorAll('#vcdList .vcd-card'))
      .find(c => text(c.querySelector('.vcd-name')) === title);
  }
  function openBox(title) {
    cardFor(title).querySelector('[data-alert]')
      .dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
    return cardFor(title);
  }
  function submit(title) {
    cardFor(title).querySelector('[data-send]')
      .dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
    return text(cardFor(title).querySelector('[data-msg]'));
  }

  let card = openBox('6-15 Day Civil Trials');
  checkTrue('box opens', !!card.querySelector('.vcd-box-on'));
  checkTrue('date field is optional and says so',
    /Leave blank for any date/.test(text(card.querySelector('.vcd-fieldlab'))));
  check('no em dash anywhere in the box',
    /\u2014/.test(card.querySelector('.vcd-box').textContent), false);
  const dateInput = card.querySelector('[data-by]');
  checkTrue('date input is bounded below', !!dateInput.getAttribute('min'));
  checkTrue('date input is bounded above', !!dateInput.getAttribute('max'));
  checkTrue('honeypot present and hidden',
    card.querySelector('[data-hp]').className.indexOf('vcd-hp') !== -1);

  // bad email
  card.querySelector('[data-email]').value = 'not-an-email';
  check('rejects a malformed address', submit('6-15 Day Civil Trials'),
    'That does not look like an email address.');

  // honeypot filled
  card = cardFor('6-15 Day Civil Trials');
  card.querySelector('[data-email]').value = 'real@example.com';
  card.querySelector('[data-hp]').value = 'spam';
  checkTrue('honeypot short-circuits silently',
    /Thanks/.test(submit('6-15 Day Civil Trials')));
  check('nothing posted', posted.length, 0);

  // a populated list with no threshold is already satisfied
  card = openBox('2 Day MVA Trials');
  card.querySelector('[data-email]').value = 'real@example.com';
  let msg = submit('2 Day MVA Trials');
  checkTrue('blank threshold on a populated list is refused',
    /already a date on 2026-09-21 that meets this criteria/.test(msg));
  checkTrue('and explains no alert was made', /No alert is created/.test(msg));
  check('nothing posted', posted.length, 0);

  // threshold later than the earliest date is also already satisfied
  card = cardFor('2 Day MVA Trials');
  card.querySelector('[data-email]').value = 'real@example.com';
  card.querySelector('[data-by]').value = '2027-01-01';
  checkTrue('a threshold beyond the earliest date is refused',
    /already a date on 2026-09-21/.test(submit('2 Day MVA Trials')));
  check('nothing posted', posted.length, 0);

  // threshold earlier than anything offered is a real subscription
  card = cardFor('2 Day MVA Trials');
  card.querySelector('[data-email]').value = 'real@example.com';
  card.querySelector('[data-by]').value = '2026-08-15';
  submit('2 Day MVA Trials');
  check('a genuine threshold posts', posted.length, 1);
  check('to court_alerts',
    posted[0].url.indexOf('/rest/v1/court_alerts') !== -1, true);
  const body = JSON.parse(posted[0].body);
  check('jurisdiction', body.jurisdiction, 'BC');
  check('location code', body.location_code, 'VA');
  check('location name', body.location_name, 'Vancouver');
  check('hearing_code is the slug', body.hearing_code, '2-day-mva-trials');
  check('threshold sent', body.wanted_by, '2026-08-15');
  check('no status field is sent', body.status, undefined);
  check('no notified_at is sent', body.notified_at, undefined);

  // an empty list accepts a blank threshold
  posted.length = 0;
  card = cardFor('6-15 Day Civil Trials');
  card.querySelector('[data-hp]').value = '';
  card.querySelector('[data-email]').value = 'real@example.com';
  submit('6-15 Day Civil Trials');
  check('empty list accepts blank threshold', posted.length, 1);
  check('wanted_by omitted entirely',
    JSON.parse(posted[0].body).wanted_by, undefined);

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
}, 300);
