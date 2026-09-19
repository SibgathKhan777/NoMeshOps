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
  var repo=document.getElementById('cb-repo'), note=document.getElementById('cb-note');
  var branchIn=document.getElementById('cb-branch'), healthIn=document.getElementById('cb-health'), startIn=document.getElementById('cb-start');
  var targetSel=document.getElementById('cb-target');
  var targetsById={}, targetOrder=[];
  var DEFAULT_REPO='https://github.com/SibgathKhan777/nomeshops-sample.git';

  function loadExamples(){
    var wrap=document.getElementById('cb-examples');
    fetch('/api/demo/examples').then(function(r){return r.json();}).then(function(d){
      d.examples.forEach(function(ex){
        var btn=document.createElement('button');
        btn.type='button'; btn.className='cb-example-chip'; btn.dataset.branch=ex.branch||'';
        btn.textContent=ex.id.charAt(0).toUpperCase()+ex.id.slice(1).replace(/([A-Z])/g,' $1');
        btn.title=ex.description;
        btn.addEventListener('click', function(){
          repo.value=DEFAULT_REPO; branchIn.value=ex.branch||'';
          Array.prototype.forEach.call(wrap.querySelectorAll('.cb-example-chip'), function(b){b.classList.remove('active');});
          btn.classList.add('active');
          note.textContent=ex.description;
        });
        wrap.appendChild(btn);
      });
    }).catch(function(){});
  }
  loadExamples();
  repo.addEventListener('input', function(){
    var wrap=document.getElementById('cb-examples');
    Array.prototype.forEach.call(wrap.querySelectorAll('.cb-example-chip'), function(b){b.classList.remove('active');});
  });

  function loadTargets(){
    fetch('/api/demo/targets').then(function(r){return r.json();}).then(function(d){
      targetSel.innerHTML='';
      var groups={};
      d.targets.forEach(function(t){ targetsById[t.id]=t; targetOrder.push(t.id);
        (groups[t.cloud]=groups[t.cloud]||[]).push(t); });
      Object.keys(groups).forEach(function(cloud){
        var og=document.createElement('optgroup'); og.label=cloud;
        groups[cloud].forEach(function(t){
          var os=t.label.split(' \u00b7 ')[1]||t.label;
          var opt=document.createElement('option'); opt.value=t.id; opt.textContent=os;
          og.appendChild(opt);
        });
        targetSel.appendChild(og);
      });
      if (targetOrder.length) cloudSub.textContent=targetsById[targetOrder[0]].label;
    }).catch(function(){ targetSel.innerHTML='<option value="">could not load targets</option>'; });
  }
  loadTargets();
  targetSel.addEventListener('change', function(){
    var t=targetsById[targetSel.value];
    if (t && sCloud.textContent==='idle') cloudSub.textContent=t.label;
  });
  function nextTarget(usedId){
    if (!targetOrder.length) return usedId;
    var i=targetOrder.indexOf(usedId);
    return targetOrder[(i+1) % targetOrder.length];
  }
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
    setSt(sLocal,'','idle');setSt(sCloud,'','idle');
    var t=targetsById[targetSel.value]; cloudSub.textContent = t ? t.label : 'not yet fingerprinted';
    outcome.hidden=true;second.hidden=true;
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
    var repoVal=(repo.value||'').trim();
    if (!repoVal){ note.textContent='Paste a public repo URL, or try one of the examples above.'; repo.focus(); return; }
    active=true; run.disabled=true; second.disabled=true; clear();
    var tok=NoMeshAuth.token();
    var params=new URLSearchParams({target:target, repo:repoVal, token:tok});
    if (branchIn.value.trim()) params.set('branch', branchIn.value.trim());
    if (healthIn.value.trim()) params.set('health_path', healthIn.value.trim());
    if (startIn.value.trim()) params.set('start_command', startIn.value.trim());
    var url='/api/demo/deploy?'+params.toString();
    var tLabel=(targetsById[target]||{}).label||target;
    note.textContent='Scanning and deploying to '+tLabel+' now — the agent has not been told what, if anything, is wrong with this repo.';
    es=new EventSource(url);
    es.addEventListener('meta',function(e){var d=JSON.parse(e.data); ln(localEl,'t-dim','$ nomeshops deploy --repo '+d.repo.replace('https://','')+(d.branch?' --branch '+d.branch:'')+' --target '+d.target); cloudSub.textContent=d.label;
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
        var nt=targetsById[nextTarget(lastTarget)];
        ocn.textContent=r.stored_fix
          ? ('That fix is now keyed by its error signature. Try '+(nt?nt.label:'a different machine')+' to see the knowledge-base hit with no model call.')
          : 'Resolved without learning anything new — the rules table or knowledge base already had it.';
        second.textContent = nt ? ('Try '+nt.label) : 'Try a different machine';
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
        // an EventSource that never got a first byte (401/403/400 from the gate) surfaces here, not as JSON
        ln(localEl,'t-fail','could not start the deploy — sign in, check the repo is a public https URL on github.com / gitlab.com / bitbucket.org / codeberg.org, or you may be out of free runs');
        es.close(); active=false; run.disabled=false; refreshAcct();
      }
    };
  }
  refreshAcct();
  var lastTarget=null;
  run.addEventListener('click',function(){ lastTarget=targetSel.value; deploy(lastTarget); });
  second.addEventListener('click',function(){
    var nt=nextTarget(lastTarget); targetSel.value=nt; lastTarget=nt; deploy(nt);
  });
  reset.addEventListener('click',function(){if(es)es.close();active=false;run.disabled=false;clear();localEl.innerHTML='<span class="ln t-dim">Waiting for a codebase…</span>';cloudEl.innerHTML='<span class="ln t-dim">Waiting for the agent…</span>';note.textContent='The agent scans whatever you paste, fingerprints the target, and deploys \u2014 it does not know in advance what, if anything, is wrong with your project.';});
})();
