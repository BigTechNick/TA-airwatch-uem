/* SPDX-License-Identifier: Apache-2.0 - Copyright 2026 Nikhil Chawla */
/*
 * TA-airwatch-uem - Inputs page.
 * Lists inputs (one per Workspace ONE UEM console) and OAuth clients; add, edit,
 * enable/disable, run now, test, delete. Talks to /services/airwatch_gps/inputs and
 * /services/airwatch_gps/clients. Client secrets are sent once and never shown again.
 */
require([
    'jquery',
    'splunkjs/mvc',
    'splunkjs/mvc/searchmanager',
    'splunkjs/mvc/simplexml/ready!'
], function ($, mvc, SearchManager) {
    'use strict';

    var APP = 'TA-airwatch-uem';
    var service = mvc.createService({ owner: 'nobody', app: APP });
    var state = { inputs: [], clients: [], clientNames: [], indexes: [], status: {} };
    var $root = $('#aw-inputs-app');
    // Token hosts of the Workspace ONE UEM SaaS regions (the https scheme is added on save).
    var TOKEN_URLS = [
        'na.uemauth.workspaceone.com/connect/token',
        'emea.uemauth.workspaceone.com/connect/token',
        'apac.uemauth.workspaceone.com/connect/token',
        'uat.uemauth.workspaceone.com/connect/token'
    ];

    // ------------------------------------------------------------ REST

    function parse(resp) {
        var data = resp && resp.data !== undefined ? resp.data : resp;
        if (typeof data === 'string') {
            try { data = JSON.parse(data); } catch (e) { /* keep text */ }
        }
        return data || {};
    }

    function errorText(err) {
        var data = parse(err);
        if (data && data.error) { return data.error; }
        if (data && data.messages && data.messages.length) { return data.messages[0].text; }
        return 'Request failed' + (err && err.status ? ' (HTTP ' + err.status + ')' : '');
    }

    function api(method, path, params, done, fail) {
        var url = '/services/airwatch_gps/' + path;
        var args = $.extend({ output_mode: 'json' }, params || {});
        var cb = function (err, resp) {
            if (err) { (fail || showError)(errorText(err)); return; }
            done(parse(resp));
        };
        if (method === 'GET') { service.get(url, args, cb); } else { service.post(url, args, cb); }
    }

    // ------------------------------------------------------------ helpers

    function el(tag, attrs, children) {
        var $e = $('<' + tag + '>');
        $.each(attrs || {}, function (k, v) {
            if (k === 'text') { $e.text(v); } else if (k === 'class') { $e.addClass(v); } else { $e.attr(k, v); }
        });
        $.each(children || [], function (_, c) { if (c) { $e.append(c); } });
        return $e;
    }

    function button(label, cls, onClick) {
        return el('button', { type: 'button', 'class': 'btn ' + (cls || ''), text: label }).on('click', onClick);
    }

    function showError(text) { flash(text, 'aw-error'); }

    // A message above the tables. Errors and Test results (stay) remain until the next
    // message or a click on them; other messages fade after 8 s.
    var flashTimer = null;

    function flash(text, cls, stay) {
        var $m = $root.find('.aw-flash');
        clearTimeout(flashTimer);
        $m.stop(true, true).removeClass('aw-error aw-ok').addClass(cls || 'aw-ok').text(text).show();
        if (cls !== 'aw-error' && !stay) { flashTimer = setTimeout(function () { $m.fadeOut(); }, 8000); }
    }

    function fmtTime(epoch) {
        if (!epoch) { return ''; }
        return new Date(epoch * 1000).toLocaleString();
    }

    function mb(bytes) {
        if (bytes === null || bytes === undefined) { return '?'; }
        return (bytes / 1048576).toFixed(bytes < 10485760 ? 1 : 0) + ' MB';
    }

    // Boolean settings are stored as 1 / 0; empty means the default.
    function isOn(value, dflt) {
        if (value === undefined || value === null || value === '') { return dflt; }
        return ['1', 'true', 'yes', 'on', 't', 'y'].indexOf(String(value).toLowerCase()) >= 0;
    }

    function seconds(interval) {
        var s = String(interval || '3600');
        return /^\d+$/.test(s) ? parseInt(s, 10) : null;          // null: a cron schedule
    }

    function scheduleText(interval) {
        var s = seconds(interval);
        if (s === null) { return 'cron: ' + interval; }
        if (s % 3600 === 0) { return 'every ' + (s / 3600 === 1 ? 'hour' : (s / 3600) + ' hours'); }
        if (s % 60 === 0) { return 'every ' + (s / 60) + ' min'; }
        return 'every ' + s + ' s';
    }

    // ------------------------------------------------------------ live status

    var statusSearch = new SearchManager({
        id: 'aw_input_status',
        search: 'index=_internal sourcetype="airwatch:gps:log" ("run done" OR "run failed" OR ERROR) ' +
            '| eval err_time=if(log_level IN ("ERROR","CRITICAL"), _time, null()), ' +
            'run_time=if(isnotnull(new), _time, null()) ' +
            '| stats max(run_time) as last_run latest(devices) as devices latest(samples) as samples ' +
            'latest(new) as new latest(took_sec) as took max(err_time) as last_error_time ' +
            'latest(message) as last_message by input',
        earliest_time: '-2d',
        latest_time: 'now',
        autostart: false,
        preview: false,
        cache: false
    });
    statusSearch.data('results', { count: 0 }).on('data', function (results) {
        var data = results.data();
        var idx = {};
        $.each(data.fields, function (i, f) { idx[f] = i; });
        // Indexed logs (search heads, or forwarders that index locally). The
        // forwarder's own log file (from the REST API) wins when it is newer.
        $.each(data.rows, function (_, row) {
            var found = {
                last_run: parseFloat(row[idx.last_run]) || null,
                devices: row[idx.devices], samples: row[idx.samples], 'new': row[idx['new']],
                took: row[idx.took],
                last_error_time: parseFloat(row[idx.last_error_time]) || null,
                last_error: row[idx.last_error_time] ? row[idx.last_message] : null
            };
            var local = state.status[row[idx.input]];
            if (!local || !local.last_run || (found.last_run && local.last_run < found.last_run)) {
                state.status[row[idx.input]] = found;
            }
        });
        renderInputs();
    });

    function liveStatus(item) {
        if (item.disabled) { return el('span', { 'class': 'aw-badge aw-off', text: 'Disabled' }); }
        var s = state.status[item.name] || {};
        var every = seconds(item.interval);
        var $wrap = el('div');
        if (!s.last_run && !s.last_error) {
            return el('span', { 'class': 'aw-badge aw-wait', text: 'Waiting for the first run' });
        }
        var cls = 'aw-good';
        var label = 'OK';
        var age = s.last_run ? (Date.now() / 1000) - s.last_run : null;
        if (s.last_error && (!s.last_run || s.last_error_time >= s.last_run)) {
            cls = 'aw-bad'; label = 'Error';
        } else if (every && age !== null && age > 2 * every + 300) {
            cls = 'aw-warn'; label = 'Late';
        }
        $wrap.append(el('span', { 'class': 'aw-badge ' + cls, text: label }));
        if (s.last_run) {
            $wrap.append(el('div', { 'class': 'aw-sub', text: 'Last run ' + fmtTime(s.last_run) + ': ' +
                (s['new'] || 0) + ' new of ' + (s.samples || 0) + ' samples, ' + (s.devices || 0) + ' devices' +
                (s.took ? ', ' + s.took + ' s' : '') }));
        }
        if (label === 'Error') { $wrap.append(el('div', { 'class': 'aw-sub aw-errtext', text: s.last_error })); }
        return $wrap;
    }

    // ------------------------------------------------------------ load

    function load() {
        api('GET', 'inputs', {}, function (data) {
            state.inputs = data.inputs || [];
            state.clientNames = data.clients || [];
            state.indexes = data.indexes || [];
            state.status = {};
            $.each(state.inputs, function (_, item) {      // read from this instance's log files
                if (item.status && (item.status.last_run || item.status.last_error)) {
                    state.status[item.name] = item.status;
                }
            });
            renderInputs();
            statusSearch.startSearch();
        });
        loadClients();
    }

    function loadClients() {
        api('GET', 'clients', {}, function (data) {
            state.clients = data.clients || [];
            renderClients();
        });
    }

    // ------------------------------------------------------------ inputs table

    function outputText(item) {
        var out = [];
        if (isOn(item.index_in_splunk, true)) { out.push('Splunk: ' + (item.index || 'default')); }
        if (isOn(item.write_cef, false)) { out.push('CEF files'); }
        return out.join(' + ') || 'none';
    }

    function renderInputs() {
        var $tb = $root.find('#aw-input-rows').empty();
        if (!state.inputs.length) {
            $tb.append(el('tr', {}, [el('td', { colspan: 7, 'class': 'aw-empty',
                text: 'No inputs yet. Add the OAuth client first (below), then add an input.' })]));
            return;
        }
        $.each(state.inputs, function (_, item) {
            var runNow = !item.disabled && seconds(item.interval) !== null;
            var actions = el('div', { 'class': 'aw-actions' }, [
                button('Edit', 'btn-small', function () { inputForm(item); }),
                button('Test', 'btn-small', function () { testInput(item.name); }),
                button(item.disabled ? 'Enable' : 'Disable', 'btn-small', function () {
                    inputAction(item.disabled ? 'enable' : 'disable', item.name);
                }),
                runNow ? button('Run now', 'btn-small', function () { inputAction('restart', item.name); }) : null,
                button('Delete', 'btn-small aw-danger', function () {
                    if (window.confirm('Delete input "' + item.name + '"? Events already indexed stay. CEF ' +
                            'files not yet read stay in ' + item.cef_path + ' for the syslog agent.')) {
                        inputAction('delete', item.name);
                    }
                })
            ]);
            var cef = item.cef && item.cef.files !== undefined
                ? el('div', { 'class': 'aw-sub', text: 'CEF: ' + item.cef.files + ' files waiting (' +
                    mb(item.cef.bytes) + ') in ' + item.cef_path }) : null;
            $tb.append(el('tr', {}, [
                el('td', {}, [el('strong', { text: item.name })]),
                el('td', {}, [
                    el('div', { text: item.console_name || (item.api_url || '').split('/')[2] || item.api_url }),
                    el('div', { 'class': 'aw-sub', text: item.api_url })
                ]),
                el('td', { text: item.client }),
                el('td', {}, [el('div', { text: outputText(item) }), cef]),
                el('td', { text: scheduleText(item.interval) }),
                el('td', {}, [liveStatus(item)]),
                el('td', {}, [actions])
            ]));
        });
    }

    function inputAction(action, name) {
        api('POST', 'inputs', { action: action, name: name }, function (data) {
            state.inputs = data.inputs || [];
            renderInputs();
            loadClients();
            flash('Input ' + name + ': ' + (action === 'restart' ? 'started a run' : action + ' done') + '.');
            if (action !== 'delete') { setTimeout(function () { statusSearch.startSearch(); }, 15000); }
        });
    }

    function testInput(name) {
        flash('Testing ' + name + ' ...', 'aw-ok', true);
        api('POST', 'inputs', { action: 'test', name: name }, function (data) {
            var t = data.test || {};
            flash('Input ' + name + ': token OK, the console reports ' + t.devices + ' devices (' + t.seconds + ' s).',
                'aw-ok', true);
        });
    }

    // ------------------------------------------------------------ forms

    function field(label, $input, help) {
        return el('div', { 'class': 'aw-field' }, [
            el('label', { text: label }), $input, help ? el('div', { 'class': 'aw-help', text: help }) : null
        ]);
    }

    function textInput(name, value, placeholder, type) {
        return el('input', { type: type || 'text', name: name, value: value || '', placeholder: placeholder || '',
            autocomplete: type === 'password' ? 'new-password' : 'off' });
    }

    function checkbox(name, label, on, help) {
        var $box = el('input', { type: 'checkbox', name: name, 'class': 'aw-check' });
        $box.prop('checked', !!on);
        return el('label', { 'class': 'aw-checkline' }, [$box, el('span', { text: ' ' + label }),
            help ? el('span', { 'class': 'aw-help', text: ' - ' + help }) : null]);
    }

    function select(name, options, value) {
        var $s = el('select', { name: name });
        $.each(options, function (_, o) {
            var opt = typeof o === 'string' ? { value: o, label: o } : o;
            $s.append(el('option', { value: opt.value, text: opt.label }));
        });
        $s.val(value);
        return $s;
    }

    function modal(title, $body, submitLabel, onSubmit) {
        var $overlay = el('div', { 'class': 'aw-overlay' });
        var $msg = el('div', { 'class': 'aw-form-error' }).hide();
        var close = function () { $overlay.remove(); };
        var $submit = button(submitLabel, 'btn-primary', function () {
            $msg.hide();
            $submit.prop('disabled', true);
            onSubmit(function (err) {
                $submit.prop('disabled', false);
                if (err) { $msg.text(err).show(); } else { close(); }
            });
        });
        var $box = el('div', { 'class': 'aw-modal' }, [
            el('h3', { text: title }), $body, $msg,
            el('div', { 'class': 'aw-modal-buttons' }, [button('Cancel', '', close), $submit])
        ]);
        $overlay.append($box).appendTo('body');
        $box.find('input:visible').first().trigger('focus');
        return $box;
    }

    function collect($body) {
        var params = {};
        $body.find('input[type=text], input[type=password], select').each(function () {
            var n = $(this).attr('name');
            if (n) { params[n] = String($(this).val() || '').trim(); }
        });
        $body.find('input[type=checkbox]').each(function () {
            params[$(this).attr('name')] = $(this).prop('checked') ? '1' : '0';
        });
        return params;
    }

    function inputForm(item) {
        var editing = !!item;
        item = item || {};
        if (!state.clientNames.length) {
            showError('Add the OAuth client first (OAuth clients, below).');
            return;
        }
        // Free text: a heavy forwarder can send to indexes that exist only on the indexers.
        var $index = textInput('index', item.index === 'default' ? '' : (item.index || ''), 'default');
        $index.attr({ list: 'aw-index-list' });
        var $indexList = el('datalist', { id: 'aw-index-list' });
        $.each(state.indexes, function (_, name) { $indexList.append(el('option', { value: name })); });
        var $adv = el('div', { 'class': 'aw-advanced' }, [
            field('Overlap (minutes)', textInput('overlap_min', item.overlap_min, '60'),
                'Each run starts this long before the previous one ended, for samples devices upload late. ' +
                'Samples already written are recognised by their ID and skipped.'),
            field('First run: look back (minutes)', textInput('first_lookback_min', item.first_lookback_min, '60')),
            field('Max look back (hours)', textInput('max_lookback_hours', item.max_lookback_hours, '24'),
                'After a long stop, a run goes back at most this far.'),
            field('Nearest city', el('div', {}, [checkbox('geocode', 'Add the nearest city', isOn(item.geocode, true),
                'country_code, city, latitude, longitude, population, state, county, country')])),
            field('CEF folder cap (MB)', textInput('cef_max_mb', item.cef_max_mb, '1024'),
                'When the CEF files reach this size (the syslog agent is not reading them), the oldest are deleted.'),
            field('Log level', select('log_level', ['INFO', 'DEBUG', 'WARNING', 'ERROR'],
                (item.log_level || 'INFO').toUpperCase()))
        ]).hide();
        var $body = el('div', { 'class': 'aw-form' }, [
            editing ? field('Name', el('div', { 'class': 'aw-static', text: item.name }))
                : field('Name', textInput('name', '', 'e.g. uem-prod'), 'Letters, digits, . _ - (used in log and file names)'),
            field('OAuth client', select('client', state.clientNames, item.client || state.clientNames[0])),
            field('API server', textInput('api_url', item.api_url, 'asNNN.awmdm.com'),
                'The console\'s REST API server (Groups & Settings > All Settings > System > Advanced > ' +
                'Site URLs > REST API URL), without /API.'),
            field('Console name (host field)', textInput('console_name', item.console_name, 'the API server\'s host name')),
            field('Schedule', textInput('interval', item.interval || '3600', '3600'),
                'Seconds between runs (60-86400), or a cron schedule such as 1 * * * * (minute 1 of every hour).'),
            field('Output', el('div', {}, [
                checkbox('index_in_splunk', 'Index in Splunk', isOn(item.index_in_splunk, true),
                    'JSON events, sourcetype airwatch:gps'),
                checkbox('write_cef', 'Write CEF', isOn(item.write_cef, false),
                    'one CEF file per run, for a syslog agent to read and delete')
            ])),
            field('Index', el('div', {}, [$index, $indexList]),
                'Any index name; it does not have to exist on this forwarder (empty = default).'),
            field('CEF folder', textInput('cef_dir', item.cef_dir, '$SPLUNK_HOME/var/log/airwatch_gps'),
                'With Write CEF: <folder>/<input name>/<input>_<date>_<time>_w0.cef. The user Splunk runs as ' +
                'must be able to write there, and the syslog agent to read it.'),
            el('a', { href: '#', 'class': 'aw-toggle', text: 'Advanced settings' }).on('click', function (e) {
                e.preventDefault(); $adv.toggle();
            }),
            $adv
        ]);
        modal(editing ? 'Edit input ' + item.name : 'Add input', $body, editing ? 'Save' : 'Add', function (done) {
            var params = collect($body);
            params.action = editing ? 'edit' : 'create';
            params.name = editing ? item.name : params.name;
            if (params.index_in_splunk === '0' && params.write_cef === '0') {
                done('Choose at least one output: Index in Splunk and/or Write CEF.');
                return;
            }
            if (!params.index) { params.index = 'default'; }
            api('POST', 'inputs', params, function (data) {
                state.inputs = data.inputs || [];
                renderInputs();
                loadClients();
                flash(editing ? 'Input ' + item.name + ' saved.' : 'Input ' + params.name +
                    ' added and enabled. Its first run starts within a minute; use Test to check the connection.');
                setTimeout(function () { statusSearch.startSearch(); }, 20000);
                done();
            }, done);
        });
    }

    function clientForm(client) {
        var editing = !!client;
        client = client || {};
        var $token = textInput('token_url', client.token_url, TOKEN_URLS[0]);
        $token.attr({ list: 'aw-token-list' });
        var $tokenList = el('datalist', { id: 'aw-token-list' });
        $.each(TOKEN_URLS, function (_, u) { $tokenList.append(el('option', { value: u })); });
        var $body = el('div', { 'class': 'aw-form' }, [
            editing ? field('Name', el('div', { 'class': 'aw-static', text: client.name }))
                : field('Name', textInput('name', '', 'e.g. uem-oauth'), 'Letters, digits, . _ -'),
            field('Token URL', el('div', {}, [$token, $tokenList]),
                'The OAuth token URL of the console\'s region; https is added when left out. Older consoles ' +
                'use uemauth.vmwservices.com.'),
            field('Client ID', textInput('client_id', client.client_id, '')),
            field('Client secret', textInput('client_secret', '', editing ? 'unchanged' : '', 'password'),
                editing ? 'Leave empty to keep the stored secret.' : 'Stored encrypted in Splunk\'s credential store; never shown again.'),
            field('Proxy (optional)', textInput('proxy_url', client.proxy_url, 'proxy.example.com:8080'),
                'host:port of an HTTP proxy for the token and API calls (no user name or password).'),
            field('CA file (optional)', textInput('ca_file', client.ca_file, '/etc/pki/tls/certs/ca-bundle.crt'),
                'PEM file on this server, when a proxy inspects TLS. Default: the server\'s trust store.'),
            field('Check', el('div', {}, [checkbox('test', 'Get a token before saving', true,
                'saves only if the console accepts the client ID and secret')]))
        ]);
        modal(editing ? 'Edit OAuth client ' + client.name : 'Add OAuth client', $body, 'Save', function (done) {
            var params = collect($body);
            params.action = 'save';
            params.name = editing ? client.name : params.name;
            api('POST', 'clients', params, function (data) {
                flash('OAuth client ' + params.name + ' saved' + (data.tested ? ' (token OK)' : '') + '.');
                load();
                done();
            }, done);
        });
    }

    // ------------------------------------------------------------ clients table

    function renderClients() {
        var $tb = $root.find('#aw-client-rows').empty();
        if (!state.clients.length) {
            $tb.append(el('tr', {}, [el('td', { colspan: 7, 'class': 'aw-empty',
                text: 'No OAuth clients. In the console: Groups & Settings > Configurations > OAuth Client ' +
                    'Management > Add (a role that can read devices), then add it here.' })]));
            return;
        }
        $.each(state.clients, function (_, c) {
            $tb.append(el('tr', {}, [
                el('td', {}, [el('strong', { text: c.name })]),
                el('td', { text: c.error || c.token_url || '' }),
                el('td', { text: c.client_id || '' }),
                el('td', { text: c.proxy_url || '-' }),
                el('td', { text: (c.saved || '') + (c.saved_by ? ' by ' + c.saved_by : '') }),
                el('td', { text: (c.used_by || []).join(', ') || '-' }),
                el('td', {}, [el('div', { 'class': 'aw-actions' }, [
                    button('Edit', 'btn-small', function () { clientForm(c); }),
                    button('Test', 'btn-small', function () {
                        api('POST', 'clients', { action: 'test', name: c.name }, function () {
                            flash('OAuth client ' + c.name + ': token OK.', 'aw-ok', true);
                        });
                    }),
                    button('Delete', 'btn-small aw-danger', function () {
                        if (window.confirm('Delete OAuth client "' + c.name + '"?')) {
                            api('POST', 'clients', { action: 'delete', name: c.name }, function () {
                                flash('OAuth client ' + c.name + ' deleted.');
                                load();
                            });
                        }
                    })
                ])])
            ]));
        });
    }

    // ------------------------------------------------------------ page

    $root.empty().append(
        el('div', { 'class': 'aw-flash', title: 'Click or press Esc to close', role: 'status', tabindex: '0' })
            .hide()
            .on('click', function () {
                // Selecting the text (to copy an error) is not a request to close it.
                if (window.getSelection && String(window.getSelection()).length) { return; }
                clearTimeout(flashTimer);
                $(this).fadeOut();
            })
            .on('keydown', function (e) {
                if (e.key === 'Escape' || e.key === 'Enter') {
                    clearTimeout(flashTimer);
                    $(this).fadeOut();
                }
            }),
        el('div', { 'class': 'aw-section' }, [
            el('div', { 'class': 'aw-head' }, [
                el('h2', { text: 'Inputs' }),
                el('div', {}, [
                    button('Refresh', '', load),
                    button('Add input', 'btn-primary', function () { inputForm(null); })
                ])
            ]),
            el('p', { 'class': 'aw-help', text: 'One input per console. Each run collects the device GPS samples ' +
                'since the previous run, with the device\'s user and the nearest city, for Splunk (sourcetype ' +
                'airwatch:gps) and/or as a CEF file. Read-only towards the console.' }),
            el('table', { 'class': 'table table-striped aw-table' }, [
                el('thead', {}, [el('tr', {}, $.map(['Name', 'Console', 'OAuth client', 'Output', 'Schedule',
                    'Status', ''], function (h) { return el('th', { text: h }); }))]),
                el('tbody', { id: 'aw-input-rows' })
            ])
        ]),
        el('div', { 'class': 'aw-section' }, [
            el('div', { 'class': 'aw-head' }, [
                el('h2', { text: 'OAuth clients' }),
                button('Add OAuth client', 'btn-primary', function () { clientForm(null); })
            ]),
            el('table', { 'class': 'table table-striped aw-table' }, [
                el('thead', {}, [el('tr', {}, $.map(['Name', 'Token URL', 'Client ID', 'Proxy', 'Saved',
                    'Used by', ''], function (h) { return el('th', { text: h }); }))]),
                el('tbody', { id: 'aw-client-rows' })
            ])
        ])
    );
    load();
});
