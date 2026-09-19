(function(){
  "use strict";
  var term=document.getElementById('term'), form=document.getElementById('form'), code=document.getElementById('code'), msg=document.getElementById('msg');
  var echo=document.getElementById('echo'), scopes=document.getElementById('scopes'), box=document.getElementById('box');
  var steps=[].slice.call(document.querySelectorAll('.auth-step'));
  var simDevice=null;
  function step(n){steps.forEach(function(s){s.hidden=s.getAttribute('data-step')!==String(n);});}
  function tl(cls,txt){var s=document.createElement('span');s.className='ln'+(cls?' '+cls:'');s.innerHTML=txt;term.appendChild(s);term.scrollTop=term.scrollHeight;}
  function say(t,k){msg.textContent=t;msg.className='msg'+(k?' '+k:'');}

  document.getElementById('sim').addEventListener('click',function(){
    term.textContent=''; say('','');
    fetch('/api/device/start',{method:'POST'}).then(function(r){return r.json();}).then(function(d){
      simDevice=d.device_code;
      tl('','$ nomeshops login');
      tl('t-dim','Opening your browser to sign in to NoMeshOps…');
      tl('','If it does not open, visit this page and enter the code:');
      tl('t-code','  '+d.user_code);
      tl('t-dim','');
      tl('t-dim','Waiting for approval… (expires in '+Math.round(d.expires_in/60)+' min)');
      code.value=d.user_code; code.focus();
      poll();
    }).catch(function(){tl('t-fail','Could not reach the server. Is it running?');});
  });

  function poll(){
    if(!simDevice)return;
    fetch('/api/device/poll?device_code='+encodeURIComponent(simDevice)).then(function(r){return r.json();}).then(function(d){
      if(d.status==='approved'){ tl('t-ok','✓ Approved · signed in as '+d.subject+' · session valid 12h'); tl('t-dim','Session cached in ~/.nomeshops/session'); tl('','$ '); simDevice=null; }
      else if(d.status==='denied'){ tl('t-fail','✗ Request declined. No session created.'); tl('','$ '); simDevice=null; }
      else if(d.status==='expired'){ tl('t-fail','Code expired. Run nomeshops login again.'); simDevice=null; }
      else setTimeout(poll,2000);
    }).catch(function(){setTimeout(poll,3000);});
  }

  code.addEventListener('input',function(){var v=code.value.toUpperCase().replace(/[^A-Z0-9-]/g,'');if(v.length===4&&code.value.length===4)v+='-';code.value=v;});
  form.addEventListener('submit',function(e){
    e.preventDefault();
    var v=code.value.toUpperCase().trim();
    fetch('/api/device/lookup?user_code='+encodeURIComponent(v)).then(function(r){
      if(!r.ok)throw new Error('bad'); return r.json();
    }).then(function(d){
      echo.textContent=d.user_code;
      scopes.innerHTML='';
      var names={deploy:'Deploy to the machines registered to your account','fixes:read':"Read your team's verified fixes",'fixes:write':'Add new verified fixes','logs:write':'Upload attempt logs for the audit trail'};
      d.requested_scopes.forEach(function(s){var li=document.createElement('li');li.innerHTML='<span class="v-dot ok"></span>'+(names[s]||s);scopes.appendChild(li);});
      window.__uc=d.user_code; step(2);
    }).catch(function(){say('That code does not match a pending sign-in. Codes expire after 10 minutes.','err');});
  });
  document.getElementById('approve').addEventListener('click',function(){
    fetch('/api/device/decision',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({user_code:window.__uc,approve:true,subject:'sibgath'})}).then(function(r){return r.json();}).then(function(){step(3);});
  });
  document.getElementById('deny').addEventListener('click',function(){
    fetch('/api/device/decision',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({user_code:window.__uc,approve:false})}).then(function(){step(4);});
  });
  document.getElementById('again').addEventListener('click',function(){step(1);code.value='';say('','');});
})();
