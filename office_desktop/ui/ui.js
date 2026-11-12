'use strict';
const get = id => document.getElementById(id);
const clock = seconds => String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0');
let filled = false;

function cell(row, text, className) {
  const td = document.createElement('td');
  td.textContent = text; if (className) td.className = className;
  row.appendChild(td); return td;
}
async function refresh() {
  const state = await window.office.state();
  get('state').textContent = ({ready: 'CONNECTED', offline: 'OFFLINE', unpaired: 'NOT PAIRED', blocked: 'SETUP NEEDED'})[state.info.status]
    + (state.info.name && state.info.status === 'ready' ? ' — ' + state.info.name : '') + (state.test ? ' (test mode)' : '');
  get('state').className = state.info.status === 'ready' ? '' : 'bad';
  get('message').textContent = state.info.message;
  const live = get('live'); live.replaceChildren();
  for (const item of state.live) {
    const row = document.createElement('tr');
    cell(row, item.topic);
    cell(row, item.state === 'recording' ? '● Recording' : item.message || item.state, item.state === 'recording' ? 'rec' : '');
    cell(row, clock(item.seconds), 'num'); cell(row, item.megabytes + ' MB', 'num');
    live.appendChild(row);
  }
  get('idle').hidden = state.live.length > 0;
  const finished = get('finished'); finished.replaceChildren();
  for (const item of state.finished) {
    const row = document.createElement('tr');
    cell(row, item.topic);
    cell(row, item.state === 'completed' ? item.message + ' · ' + clock(item.seconds) : 'Failed: ' + item.message, item.state === 'completed' ? 'ok' : 'bad');
    const where = cell(row, '', 'path');
    if (item.output) {
      const link = document.createElement('button');
      link.className = 'plain'; link.textContent = 'Show file'; link.onclick = () => window.office.open(item.output);
      where.appendChild(link);
    }
    finished.appendChild(row);
  }
  get('none').hidden = state.finished.length > 0;
  get('save').textContent = state.paired ? 'Save settings' : 'Connect';
  get('save').disabled = state.live.length > 0;
  if (!filled && state.settings) {
    filled = true;
    for (const key of ['server', 'outputDir', 'capacity', 'width', 'height']) get(key).value = state.settings[key];
    get('token').placeholder = 'Paired. Leave blank to keep the current code';
  }
}
get('pair').addEventListener('submit', async event => {
  event.preventDefault();
  get('error').textContent = '';
  try {
    await window.office.pair({server: get('server').value, token: get('token').value, outputDir: get('outputDir').value,
      capacity: Number(get('capacity').value), width: Number(get('width').value), height: Number(get('height').value)});
    get('token').value = ''; filled = false;
  } catch (error) { get('error').textContent = error.message.replace(/^Error invoking remote method '[^']+': (Error: )?/, ''); }
  refresh();
});
get('folder').addEventListener('click', () => window.office.open(''));
refresh(); setInterval(refresh, 1000);
