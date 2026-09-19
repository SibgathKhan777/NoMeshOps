(function(){
  "use strict";
  var quotaLine = document.getElementById('quota-line');
  var acctSlot = document.getElementById('acct-slot');
  var meCache = null;

  function refreshAcct(){
    if (acctSlot) NoMeshAuth.mountAccountPill(acctSlot, {onChange: syncGate});
    NoMeshAuth.me(true).then(function(d){ meCache = d; syncGate(d); });
  }
  function syncGate(d){
    meCache = d;
    var runBtn = document.getElementById('cb-run'), secondBtn = document.getElementById('cb-second');
    if (!d){
      quotaLine.textContent = 'Sign in to run the live demo \u2014 free accounts get 3 runs.';
      if (runBtn){ runBtn.textContent = 'Sign in to deploy'; }
      return;
    }
    var left = d.demo_runs_remaining;
    quotaLine.textContent = left > 0
      ? (d.email + ' \u00b7 ' + left + ' of ' + d.demo_runs_limit + ' free demo runs left')
      : (d.email + ' \u00b7 all ' + d.demo_runs_limit + ' free demo runs used on this account');
    if (runBtn) runBtn.textContent = left > 0 ? 'Scan & deploy' : 'No runs left';
    if (secondBtn && left <= 0) secondBtn.hidden = true;
  }

  var localEl=document.getElementById('m-local'), cloudEl=document.getElementById('m-cloud');
  var sLocal=document.getElementById('s-local'), sCloud=document.getElementById('s-cloud'), cloudSub=document.getElementById('s-cloud-sub');
  var run=document.getElementById('cb-run'), second=document.getElementById('cb-second'), reset=document.getElementById('cb-reset');
  var scen=document.getElementById('cb-scenario'), repo=document.getElementById('cb-repo'), note=document.getElementById('cb-note');
  var outcome=document.getElementById('outcome'), ocb=document.getElementById('oc-badge'), oct=document.getElementById('oc-text'), ocn=document.getElementById('oc-note');
  var rungs={rules:q('rules'),kb:q('kb'),llm:q('llm')}, rst={rules:id('rs-rules'),kb:id('rs-kb'),llm:id('rs-llm')};
  var vmap={}; ['install','import','start','health'].forEach(function(k){vmap[k]=document.querySelector('.verify-list li[data-v="'+k+'"]');});
  function q(n){return document.querySelector('.rung[data-rung="'+n+'"]');} function id(n){return document.getElementById(n);}
  var CLS={info:'t-dim',dim:'t-dim',warn:'t-llm',error:'t-fail',success:'t-ok',rules:'t-rules',kb:'t-kb',knowledge:'t-kb',llm:'t-llm',bedrock:'t-llm',signature:'t-dim',fingerprint:'t-dim',store:'t-kb'};
  var es=null, active=false;

  function setSt(el,cls,txt){el.className='m-state mono'+(cls?' '+cls:'');el.textContent=txt;}
  function ln(el,cls,txt){var s=document.createElement('span');s.className='ln new'+(cls?' '+cls:'');s.textContent=txt;el.appendChild(s);el.scrollTop=el.scrollHeight;}
  function rung(n,st,label){var r=rungs[n];if(!r)return;r.className='rung '+(st==='on'?'on':st==='hit'?'on hit':st);rst[n].textContent=label||'';}
  function vstep(k,st){if(vmap[k])vmap[k].className=st;}
  function clear(){
    localEl.textContent='';cloudEl.textContent='';
    Object.keys(rungs).forEach(function(k){rungs[k].className='rung';rst[k].textContent='—';});
    Object.keys(vmap).forEach(function(k){vmap[k].className='';});
    setSt(sLocal,'','idle');setSt(sCloud,'','idle');cloudSub.textContent='not yet fingerprinted';outcome.hidden=true;second.hidden=true;
  }

  // classify an orchestrator event onto the two panels + agent widgets
  function handle(ev){
    var node=ev.node||'', msg=ev.msg||'', lvl=ev.level||'info', cls=CLS[lvl]||CLS[node]||'t-dim';
    var t=(typeof ev.t==='number')?('  '+ev.t.toFixed(1)+'s ').slice(-8):'        ';
    var line=t+' '+(node+'           ').slice(0,11)+' '+msg;
    // routing: fingerprint/deploy/verify/store happen ON the machine; scan/rules/knowledge/bedrock are the control plane
    var onCloud = (node==='fingerprint'||node==='deploy'||node==='verify');
    (onCloud?function(a,b){ln(cloudEl,a,b);}:function(a,b){ln(localEl,a,b);})(cls,line);

    if(node==='scan')setSt(sLocal,'busy','scanning');
    if(node==='fingerprint'){ setSt(sCloud,'busy','fingerprinting'); var m=msg.match(/:\s*(.+?)\s*\(/); if(m)cloudSub.textContent=m[1].slice(0,52); }
    if(node==='deploy'){ setSt(sLocal,'busy','deploying');
      if(/attempt \d+:/.test(msg))setSt(sCloud,'busy',(msg.match(/attempt \d+/)||['attempt'])[0]);
      if(/install ok/.test(msg)){vstep('install','ok');}
      if(/failed/.test(msg))setSt(sCloud,'fail','failed');
      if(/applying fix/.test(msg)){var src=/knowledge/.test(msg)?'kb':/bedrock|llm/.test(msg)?'llm':'rules'; rung(src,'hit','applying');}
    }
    if(node==='check'){ if(/predicted/.test(msg)&&/0 predicted/.test(msg)===false){rung('rules','hit','predicted');} else rung('rules','on','ok'); }
    if(node==='rules'){ rung('rules', /HIT/.test(msg)?'hit':'miss', /HIT/.test(msg)?'hit':'miss'); }
    if(node==='knowledge'){ var hit=/HIT/.test(msg); rung('kb',hit?'hit':'miss', hit?(msg.match(/in \d+ ?ms/)||['hit'])[0]:'miss'); }
    if(node==='bedrock'){ rung('llm','checking','asking…'); if(/proposed/.test(msg))rung('llm','hit','1 call'); }
    if(node==='verify'){ setSt(sCloud,'busy','verifying');
      if(/import ok/.test(msg)){vstep('import','ok');vstep('start','ok');vstep('health','ok');}
      if(/import check FAILED/.test(msg))vstep('import','fail');
      if(/health check FAILED|exited before/.test(msg)){vstep('start','fail');vstep('health','fail');}
    }
    if(node==='finalize'){ if(/VERIFIED/.test(msg)){setSt(sCloud,'ok','verified');setSt(sLocal,'ok','done');} else {setSt(sCloud,'fail','failed');setSt(sLocal,'fail','failed');} }
  }

  function deploy(target){
    if(active)return;
    if (!meCache){ note.textContent='Sign in to run the live demo.'; NoMeshAuth.open('signup', function(){ refreshAcct(); }); return; }
    if (meCache.demo_runs_remaining <= 0){ note.textContent="You've used all "+meCache.demo_runs_limit+" free demo runs on this account."; return; }
    active=true; run.disabled=true; second.disabled=true; clear();
    var tok=NoMeshAuth.token();
    var url='/api/demo/deploy?scenario='+encodeURIComponent(scen.value)+'&target='+target+'&token='+encodeURIComponent(tok);
    note.textContent='Deploying to '+target+' now. Streaming live from the server…';
    es=new EventSource(url);
    es.addEventListener('meta',function(e){var d=JSON.parse(e.data); ln(localEl,'t-dim','$ nomeshops deploy --repo '+d.repo.replace('https://','')+' --target '+d.target); cloudSub.textContent=d.label;
      if (typeof d.demo_runs_used === 'number'){ meCache.demo_runs_used = d.demo_runs_used; meCache.demo_runs_remaining = Math.max(0, d.demo_runs_limit - d.demo_runs_used); syncGate(meCache); }
    });
    es.addEventListener('log',function(e){handle(JSON.parse(e.data));});
    es.addEventListener('result',function(e){
      var r=JSON.parse(e.data); es.close(); active=false; run.disabled=false; second.disabled=false;
      outcome.hidden=false;
      var ok=r.deploy_success;
      ocb.className='oc-badge mono '+(ok?'ok':'fail'); ocb.textContent=ok?'verified':'failed';
      if(ok){
        oct.textContent='Deploy verified end to end in '+r.duration_s+'s'+(r.stored_fix?', and the fix was stored.':'.');
        ocn.textContent=r.stored_fix?'That fix is now keyed by its error signature. Run it on a second machine to see the knowledge-base hit with no model call.':'Resolved without learning anything new — the rules table or knowledge base already had it.';
        second.hidden=false;
      } else {
        oct.textContent='Deploy failed cleanly: '+(r.failure_reason||'no verified fix');
        ocn.textContent='Nothing was stored. A failure the tool cannot verify is never learned, so the knowledge base stays clean.';
        second.hidden=true;
      }
      outcome.scrollIntoView({block:'nearest',behavior:'smooth'});
    });
    es.onerror=function(){
      if(active){
        // an EventSource that never got a first byte (401/403 from the gate) surfaces here, not as JSON
        ln(localEl,'t-fail','could not start the deploy — sign in, or you may be out of free runs');
        es.close(); active=false; run.disabled=false; refreshAcct();
      }
    };
  }
  refreshAcct();
  run.addEventListener('click',function(){deploy('cloud-1');});
  second.addEventListener('click',function(){deploy('cloud-2');});
  reset.addEventListener('click',function(){if(es)es.close();active=false;run.disabled=false;clear();localEl.innerHTML='<span class="ln t-dim">Waiting for a codebase…</span>';cloudEl.innerHTML='<span class="ln t-dim">Waiting for the agent…</span>';note.textContent='Pick a project and press Scan & deploy.';});
})();
