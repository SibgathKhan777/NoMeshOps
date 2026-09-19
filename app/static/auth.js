/* Shared account auth: email + password, JWT-less bearer token in localStorage, and a modal used
   by every page. Exposes window.NoMeshAuth. */
(function(){
  "use strict";
  var TOKEN_KEY = "nomeshops.token";
  var cachedMe = null;

  function token(){ try{ return localStorage.getItem(TOKEN_KEY) || ""; }catch(e){ return ""; } }
  function setToken(t){ try{ if(t) localStorage.setItem(TOKEN_KEY, t); else localStorage.removeItem(TOKEN_KEY); }catch(e){} }

  function me(force){
    if(cachedMe && !force) return Promise.resolve(cachedMe);
    var t = token();
    if(!t){ cachedMe = null; return Promise.resolve(null); }
    return fetch("/api/auth/me", {headers:{authorization:"Bearer "+t}}).then(function(r){
      if(!r.ok){ setToken(""); cachedMe = null; return null; }
      return r.json();
    }).then(function(d){ cachedMe = d; return d; }).catch(function(){ return null; });
  }

  function logout(){
    var t = token();
    setToken(""); cachedMe = null;
    if(t) fetch("/api/auth/logout", {method:"POST", headers:{authorization:"Bearer "+t}}).catch(function(){});
  }

  /* ---------- modal ---------- */
  var overlay, card, tabSignup, tabLogin, form, emailIn, passIn, err, submitBtn, subLine;
  var mode = "signup", onSuccessCb = null;

  function buildModal(){
    if(overlay) return;
    overlay = document.createElement("div");
    overlay.className = "auth-overlay"; overlay.hidden = true;
    overlay.innerHTML =
      '<div class="auth-card" role="dialog" aria-modal="true">' +
        '<button class="auth-close" type="button" aria-label="Close">×</button>' +
        '<div class="eyebrow">Hosted account</div>' +
        '<h2 id="auth-h">Create your account</h2>' +
        '<p class="sub" id="auth-sub">Free accounts get 3 live demo runs.</p>' +
        '<div class="auth-tabs">' +
          '<div class="auth-tab active" data-m="signup">Sign up</div>' +
          '<div class="auth-tab" data-m="login">Log in</div>' +
        '</div>' +
        '<form id="auth-form">' +
          '<div class="field"><label for="auth-email">Email</label><input id="auth-email" type="email" autocomplete="email" placeholder="you@example.com" required></div>' +
          '<div class="field"><label for="auth-pass">Password</label><input id="auth-pass" type="password" autocomplete="new-password" placeholder="at least 8 characters" required></div>' +
          '<p class="auth-err" id="auth-err"></p>' +
          '<div class="auth-actions"><button class="btn btn-primary" id="auth-submit" type="submit">Create account</button></div>' +
        '</form>' +
      '</div>';
    document.body.appendChild(overlay);
    card = overlay.querySelector(".auth-card");
    tabSignup = overlay.querySelector('[data-m="signup"]');
    tabLogin = overlay.querySelector('[data-m="login"]');
    form = overlay.querySelector("#auth-form");
    emailIn = overlay.querySelector("#auth-email");
    passIn = overlay.querySelector("#auth-pass");
    err = overlay.querySelector("#auth-err");
    submitBtn = overlay.querySelector("#auth-submit");
    subLine = overlay.querySelector("#auth-sub");

    overlay.addEventListener("click", function(e){ if(e.target === overlay) close(); });
    overlay.querySelector(".auth-close").addEventListener("click", close);
    tabSignup.addEventListener("click", function(){ setMode("signup"); });
    tabLogin.addEventListener("click", function(){ setMode("login"); });
    document.addEventListener("keydown", function(e){ if(e.key === "Escape" && !overlay.hidden) close(); });

    form.addEventListener("submit", function(e){
      e.preventDefault();
      err.textContent = ""; submitBtn.disabled = true;
      var email = emailIn.value.trim(), password = passIn.value;
      var url = mode === "signup" ? "/api/auth/signup" : "/api/auth/login";
      fetch(url, {method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({email:email, password:password})})
        .then(function(r){ return r.json().then(function(d){ return {ok:r.ok, d:d}; }); })
        .then(function(res){
          submitBtn.disabled = false;
          if(!res.ok){ err.textContent = res.d.detail || "That didn't work. Try again."; return; }
          setToken(res.d.token); cachedMe = res.d;
          close();
          if(onSuccessCb) onSuccessCb(res.d);
        }).catch(function(){ submitBtn.disabled = false; err.textContent = "Could not reach the server."; });
    });
  }

  function setMode(m){
    mode = m;
    tabSignup.classList.toggle("active", m === "signup");
    tabLogin.classList.toggle("active", m === "login");
    overlay.querySelector("#auth-h").textContent = m === "signup" ? "Create your account" : "Log in";
    subLine.textContent = m === "signup" ? "Free accounts get 3 live demo runs." : "Welcome back.";
    submitBtn.textContent = m === "signup" ? "Create account" : "Log in";
    passIn.autocomplete = m === "signup" ? "new-password" : "current-password";
    err.textContent = "";
  }

  function open(initialMode, onSuccess){
    buildModal();
    setMode(initialMode || "signup");
    onSuccessCb = onSuccess || null;
    err.textContent = ""; form.reset();
    overlay.hidden = false;
    setTimeout(function(){ emailIn.focus(); }, 30);
  }
  function close(){ if(overlay) overlay.hidden = true; }

  /* ---------- header account pill: call once per page with a mount element ---------- */
  function mountAccountPill(el, opts){
    opts = opts || {};
    function render(){
      me().then(function(d){
        el.innerHTML = "";
        if(d){
          var pill = document.createElement("button");
          pill.className = "acct-pill"; pill.type = "button";
          pill.textContent = d.email + " · " + d.demo_runs_remaining + "/" + d.demo_runs_limit + " runs left";
          pill.title = "Click to sign out";
          pill.addEventListener("click", function(){ logout(); render(); if(opts.onChange) opts.onChange(null); });
          el.appendChild(pill);
        } else {
          var a = document.createElement("button");
          a.className = "acct-pill"; a.type = "button"; a.textContent = "Sign in";
          a.addEventListener("click", function(){ open("login", function(d){ render(); if(opts.onChange) opts.onChange(d); }); });
          el.appendChild(a);
        }
      });
    }
    render();
    return render;
  }

  window.NoMeshAuth = { token: token, me: me, logout: logout, open: open, close: close, mountAccountPill: mountAccountPill };
})();
