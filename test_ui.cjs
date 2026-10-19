const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(`${__dirname}/sysmon.py`, 'utf8');
const js = source.split('<script>')[1].split('</script>')[0];
(async () => {
    let calls = 0, resolveFetch;
    const timers = [];
    const context = vm.createContext({
        Map, AbortController,
        setTimeout: cb => { timers.push(cb); return timers.length; },
        clearTimeout: () => {},
        fetch: () => { calls++; return new Promise(resolve => { resolveFetch = resolve; }); }
    });
    vm.runInContext(js.slice(js.indexOf('    const pendingGETs'), js.indexOf('    // global focus')) + '\nglobalThis.getJSON = getJSON;', context);
    const a = context.getJSON('/api/stats');
    const b = context.getJSON('/api/stats');
    assert.equal(a, b);
    assert.equal(calls, 1);
    resolveFetch({ ok: true, json: async () => ({ value: 1 }) });
    assert.deepEqual(await a, { value: 1 });
    const c = context.getJSON('/api/stats');
    assert.equal(calls, 2);
    resolveFetch({ ok: false, status: 500 });
    await assert.rejects(c, /HTTP 500/);
    const d = context.getJSON('/api/stats');
    assert.equal(calls, 3);
    resolveFetch({ ok: true, json: async () => ({}) });
    await d;

    let tick, finish, runs = 0;
    const document = { hidden: false };
    const polling = vm.createContext({ document, setInterval: cb => { tick = cb; } });
    vm.runInContext(js.slice(js.indexOf('    function poll(task'), js.indexOf('    poll(update')) + '\nglobalThis.poll = poll;', polling);
    polling.poll(() => { runs++; return new Promise(resolve => { finish = resolve; }); }, 2000);
    await tick();
    assert.equal(runs, 1, 'slow refresh must not overlap');
    finish();
    await new Promise(resolve => setImmediate(resolve));
    document.hidden = true;
    await tick();
    assert.equal(runs, 1, 'hidden pages must not poll');
    document.hidden = false;
    const next = tick();
    assert.equal(runs, 2, 'visible page resumes polling');
    finish();
    await next;
    const demand = [];
    let networkOpen = false;
    const visibility = { hidden: false };
    const controls = vm.createContext({
        crypto: { randomUUID: () => 'test-viewer' },
        document: visibility,
        isOpen: () => networkOpen,
        postJSON: async (url, body) => { demand.push(body); }
    });
    vm.runInContext(js.slice(js.indexOf('    const paused ='), js.indexOf('    let openState')) +
        '\nglobalThis.syncActivity = syncActivity; globalThis.paused = paused; globalThis.enable = () => { activityReady = true; };', controls);
    await controls.syncActivity();
    assert.equal(demand.length, 0, 'initialization must not activate collectors');
    controls.enable();
    await controls.syncActivity();
    assert.equal(demand.at(-1).network, false);
    assert.equal(demand.at(-1).fileops, false, 'disk collection starts paused');
    networkOpen = true;
    await controls.syncActivity();
    assert.equal(demand.at(-1).network, true, 'expansion activates network collection');
    controls.paused.disk = false;
    await controls.syncActivity();
    assert.equal(demand.at(-1).fileops, true, 'Play activates file collection');
    visibility.hidden = true;
    await controls.syncActivity();
    assert.equal(demand.at(-1).network, false);
    assert.equal(demand.at(-1).fileops, false);
    console.log('UI checks passed: request sharing, error recovery, no overlapping polling, hidden-page suspension, explicit collector activation');
})().catch(error => { console.error(error); process.exitCode = 1; });
