"""OSINT STATION look for SHADE's browser console (same tokens as the SHADE
desktop and FIGHT: StationWatch/fight_theme.py)."""
import base64
from importlib import resources

TOKENS = {'bg': '#101923', 'panel': '#192735', 'text': '#e7edf4', 'muted': '#9bafc2', 'accent': '#59d8ce',
          'heading': '#24384a', 'tab': '#182631', 'hover': '#285268', 'selected': '#205d6a',
          'disabled': '#637789', 'warning': '#e0a45c', 'border': '#2c4256', 'danger': '#e07a6c'}
FONT = "'Segoe UI', system-ui, -apple-system, sans-serif"
MONO = "Consolas, 'Cascadia Mono', ui-monospace, monospace"


def logo_uri():
    try:
        data = resources.files('shade_node').joinpath('assets/web-header-logo.png').read_bytes()
        return 'data:image/png;base64,' + base64.b64encode(data).decode('ascii')
    except (OSError, FileNotFoundError):
        return ''


CSS = """
:root{--bg:%(bg)s;--panel:%(panel)s;--text:%(text)s;--muted:%(muted)s;--accent:%(accent)s;--heading:%(heading)s;--tab:%(tab)s;
--hover:%(hover)s;--selected:%(selected)s;--disabled:%(disabled)s;--warning:%(warning)s;--border:%(border)s;--danger:%(danger)s}
*{box-sizing:border-box}
[hidden]{display:none!important}
#q{width:220px}#min,#max_age{width:92px}
html,body{margin:0;background:var(--bg);color:var(--text);font:400 14px/1.45 %(font)s;height:100%%}
.osint-bar{display:flex;align-items:center;gap:14px;padding:10px 20px;background:var(--tab);border-bottom:1px solid var(--border)}
.osint-bar img{width:40px;height:40px;border-radius:8px}
.osint-name{display:flex;flex-direction:column;line-height:1.2}
.osint-name strong{font:600 21px/1.1 'Segoe UI Semibold',%(font)s;color:var(--accent);letter-spacing:.04em}
.osint-name span{color:var(--muted);font-size:12.5px}
.osint-suite{margin-left:auto;display:flex;align-items:center;gap:12px;color:var(--muted);font-size:12px}
.tabs{display:flex;gap:2px;padding:0 20px;background:var(--tab);border-bottom:1px solid var(--border)}
.tabs a{padding:9px 16px;color:var(--muted);text-decoration:none;border-radius:6px 6px 0 0}
.tabs a.on{background:var(--panel);color:var(--text)}
.tabs a.soon{opacity:.45;pointer-events:none}
main{padding:14px 20px 20px}
.bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:10px}
.bar .grow{flex:1}
button,.btn{background:var(--heading);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:7px 13px;
 font:600 13px/1.2 'Segoe UI Semibold',%(font)s;cursor:pointer}
button:hover{background:var(--hover);border-color:var(--accent)}
button:disabled{color:var(--disabled);cursor:not-allowed;background:var(--panel);border-color:var(--border)}
button.primary{border-color:var(--accent)}
button.danger:hover{border-color:var(--danger)}
select,input{background:var(--panel);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:6px 8px;font:inherit}
input[type=checkbox]{accent-color:var(--accent);width:16px;height:16px;padding:0;vertical-align:middle}
select:focus,input:focus,button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
label.inline{color:var(--muted);display:inline-flex;align-items:center;gap:6px}
.split{display:grid;grid-template-columns:minmax(0,1fr) 400px;gap:14px;height:calc(100vh - 205px);min-height:420px}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:6px;overflow:auto}
table{border-collapse:collapse;width:100%%}
th{position:sticky;top:0;background:var(--heading);color:var(--text);text-align:left;font:600 12.5px/1.2 'Segoe UI Semibold',%(font)s;
 padding:9px 8px;cursor:pointer;white-space:nowrap;user-select:none;z-index:1}
th.nosort{cursor:default}
td{padding:7px 8px;border-top:1px solid var(--border);vertical-align:top}
tr.row{cursor:pointer}tr.row:hover td{background:#1d2e3e}
tr.row.sel td{background:var(--selected)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.title{min-width:260px}
td.nowrap,th{white-space:nowrap}
.muted{color:var(--muted)}
.field{margin:8px 0;padding:6px 10px;border-left:3px solid var(--accent);background:var(--bg);color:var(--text);font-size:13px}
.switch{position:relative;display:inline-block;width:36px;height:20px}.switch input{opacity:0;width:0;height:0}
.switch span{position:absolute;inset:0;background:var(--border);border-radius:20px;cursor:pointer;transition:.15s}
.switch span:before{content:"";position:absolute;width:14px;height:14px;left:3px;top:3px;background:var(--text);border-radius:50%%;transition:.15s}
.switch input:checked+span{background:var(--accent)}.switch input:checked+span:before{transform:translateX(16px)}
.packs{display:flex;flex-wrap:wrap;gap:6px 18px;margin:8px 0}
.colf{background:none!important;border:0!important;padding:0 2px!important;margin-left:4px;color:var(--muted);cursor:pointer;font-size:11px;min-width:0}
.colf.on{color:var(--accent);font-weight:700}
#colpop{position:absolute;z-index:60;min-width:220px;max-width:320px;max-height:360px;overflow:auto;background:var(--panel);border:1px solid var(--accent);border-radius:6px;padding:10px 12px;box-shadow:0 8px 24px rgba(0,0,0,.45)}
#colpop label{display:flex;gap:8px;align-items:center;padding:3px 0;white-space:nowrap;cursor:pointer}
#colpop .colbtns{display:flex;gap:8px;margin-top:8px}
.tag{display:inline-block;padding:1px 6px;border-radius:3px;font-size:11.5px;letter-spacing:.05em;border:1px solid var(--border);color:var(--muted)}
.tag.tx{color:var(--accent);border-color:var(--accent)}.tag.new{color:var(--text)}.tag.warn{color:var(--warning);border-color:var(--warning)}
.detail{padding:14px 16px}
.detail h2{font:600 16px/1.3 'Segoe UI Semibold',%(font)s;margin:0 0 8px}
.detail pre{font:12.5px/1.5 %(mono)s;white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg);border:1px solid var(--border);
 border-radius:5px;padding:10px 12px;margin:10px 0}
.actions{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.links{margin:8px 0}.links a{color:var(--accent);display:block;margin:3px 0;overflow-wrap:anywhere}
.pager{display:flex;align-items:center;gap:8px;padding:8px;border-top:1px solid var(--border);position:sticky;bottom:0;background:var(--panel)}
.status{display:flex;gap:16px;align-items:center;padding:6px 20px;border-top:1px solid var(--border);background:var(--tab);color:var(--muted);
 font-size:12.5px;position:fixed;bottom:0;left:0;right:0}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;border:1px solid var(--accent);color:var(--accent);cursor:pointer}
.pill.warn{border-color:var(--warning);color:var(--warning)}
.spin{display:inline-block;width:10px;height:10px;border:2px solid var(--accent);border-right-color:transparent;border-radius:50%%;
 animation:spin .8s linear infinite;vertical-align:-1px}@keyframes spin{to{transform:rotate(360deg)}}
.empty{padding:30px;text-align:center;color:var(--muted)}
.toast{z-index:50;position:fixed;right:20px;bottom:44px;max-width:460px;background:var(--panel);border:1px solid var(--accent);border-radius:6px;
 padding:10px 14px;box-shadow:0 6px 24px rgba(0,0,0,.4);white-space:pre-wrap}
.toast.err{border-color:var(--danger)}
.modal-bg{position:fixed;inset:0;background:rgba(5,10,15,.72);display:flex;align-items:flex-start;justify-content:center;padding-top:8vh;z-index:20}
.modal{background:var(--panel);border:1px solid var(--accent);border-radius:8px;width:min(720px,94vw);max-height:84vh;overflow:auto;padding:18px 20px;
 box-shadow:0 12px 40px rgba(0,0,0,.5)}
.modal h3{margin:0 0 10px;font:600 17px/1.3 'Segoe UI Semibold','Segoe UI', system-ui, -apple-system, sans-serif;color:var(--accent)}
.modal label{display:block;color:var(--muted);margin:10px 0 4px}
.modal input,.modal textarea,.modal select{width:100%%;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:8px;font:inherit}
.modal textarea{min-height:150px;font:13px/1.5 Consolas, 'Cascadia Mono', ui-monospace, monospace;resize:vertical}
.modal .row{display:flex;gap:8px;justify-content:flex-end;margin-top:14px;flex-wrap:wrap}
.modal .row .grow{flex:1}
.modal .note{color:var(--warning);font-size:13px;margin:8px 0}
.actions.secondary button{background:var(--panel)}
@media (max-width:980px){.split{grid-template-columns:1fr;height:auto}.panel{max-height:none}.osint-suite .who{display:none}}
""" % dict(TOKENS, font=FONT, mono=MONO)
