'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('src/platforms/nolworld.py', 'utf8');
const match = source.match(/ONESTOP_SCHEDULE_JS = r"""([\s\S]*?)"""/);
assert.ok(match, 'ONESTOP_SCHEDULE_JS must be present');
const bookingMatch = source.match(/BOOKING_STEP_JS = r"""([\s\S]*?)"""/);
assert.ok(bookingMatch, 'BOOKING_STEP_JS must be present');
new vm.Script(bookingMatch[1].replace('__CONFIG__', JSON.stringify({
  dates: [],
  scheduleTargets: [],
  tiers: [],
  seatTypes: ['STANDING', 'SEATED'],
  zones: [],
  customBlocks: [],
  numSeats: 1,
  dateFallback: false,
  areaFallback: false,
  dateIndex: 0,
  areaIndex: 0,
})));

const state = {
  dateSelected: false,
  timeSelected: false,
  nextClicks: 0,
};

class FakeElement {
  constructor({text = '', className = '', dataset = {}, attrs = {}, onClick} = {}) {
    this.textContent = text;
    this.className = className;
    this.dataset = dataset;
    this.attrs = attrs;
    this.disabled = false;
    this.value = '';
    this.parentElement = null;
    this.onClick = onClick;
  }

  getAttribute(name) {
    return this.attrs[name] ?? null;
  }

  querySelector() {
    return null;
  }

  scrollIntoView() {}

  getBoundingClientRect() {
    return {left: 0, top: 0, width: 120, height: 36};
  }

  click() {
    if (this.onClick) this.onClick();
  }
}

const month = new FakeElement({text: 'July 2026'});
const date = new FakeElement({
  text: '12',
  className: 'EntCalendar_dateButton',
  dataset: {date: '2026-07-12'},
  onClick: () => {
    state.dateSelected = true;
    date.attrs['aria-selected'] = 'true';
  },
});
const time = new FakeElement({
  text: '17:00',
  className: 'TimeBlock_timeButton',
  onClick: () => {
    state.timeSelected = true;
    time.attrs['aria-pressed'] = 'true';
  },
});
const previous = new FakeElement({text: 'Previous'});
const next = new FakeElement({
  text: 'Next',
  onClick: () => {
    state.nextClicks += 1;
  },
});

const document = {
  querySelector(selector) {
    return selector.includes('EntCalendar_month') ? month : null;
  },
  querySelectorAll(selector) {
    if (selector.includes('EntCalendar_date')) return [date];
    if (selector.includes('TimeBlock_timeButton')) {
      return state.dateSelected ? [time] : [];
    }
    if (
      selector.includes('ScheduleContent_footerButton') ||
      selector.includes('EntButton_primary')
    ) {
      return state.timeSelected ? [previous, next] : [];
    }
    return [];
  },
};

const context = {
  document,
  getComputedStyle: () => ({
    display: 'block',
    visibility: 'visible',
    opacity: '1',
  }),
};
const config = {
  dates: ['20260712'],
  scheduleTargets: [{date: '20260712', time: '17:00'}],
  dateFallback: false,
  dateIndex: 0,
};
const script = match[1].replace('__CONFIG__', JSON.stringify(config));

const first = vm.runInNewContext(script, context);
assert.deepEqual(
  JSON.parse(JSON.stringify(first)),
  {action: 'date_selected', date: '20260712'},
);

const second = vm.runInNewContext(script, context);
assert.equal(second.action, 'time_selected');
assert.equal(second.date, '20260712');
assert.equal(second.time, '17:00');

const third = vm.runInNewContext(script, context);
assert.equal(third.action, 'schedule_submitted');
assert.equal(third.date, '20260712');
assert.equal(third.time, '17:00');
assert.equal(state.nextClicks, 1);

console.log('NOL schedule DOM state test: OK');
