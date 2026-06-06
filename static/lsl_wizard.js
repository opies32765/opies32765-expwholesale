/* LSL_WIZARD_2026_06_05 — guided "Book to LSL" wizard (static, hot-editable).
   One prompt per screen, Back anywhere, smart pre-fill, rare branches only when
   triggered. Stages to /api/bid/<id>/lsl-stage (no live LSL write yet).
   Sets window.__ewHoldReload while open so the page never refreshes the card away. */
(function () {
  'use strict';
  var BID = (typeof BID_ID !== 'undefined' ? BID_ID : (typeof BID_ID_LSL !== 'undefined' ? BID_ID_LSL : (window.BID_ID || window.BID_ID_LSL)));
  var C = { bg:'#0b0f19', card:'#0f1629', line:'#1e293b', accent:'#7c3aed', accent2:'#a78bfa',
            text:'#e2e8f0', sub:'#94a3b8', green:'#4ade80', red:'#f87171', amber:'#fbbf24' };
  var ctx = null, state = null, cur = null, hist = [], reps = null, host = null;

  function injectCss() {
    if (document.getElementById('lslw-css')) return;
    var s = document.createElement('style'); s.id = 'lslw-css';
    s.textContent = [
      '#lslw-ov{position:fixed;inset:0;z-index:99999;background:rgba(2,6,16,.86);display:flex;align-items:flex-end;justify-content:center}',
      '@media(min-width:640px){#lslw-ov{align-items:center}}',
      '#lslw-card{width:100%;max-width:480px;max-height:94vh;display:flex;flex-direction:column;background:'+C.card+';border:1px solid '+C.line+';border-radius:16px 16px 0 0;overflow:hidden}',
      '@media(min-width:640px){#lslw-card{border-radius:16px}}',
      '.lslw-top{display:flex;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid '+C.line+'}',
      '.lslw-back{cursor:pointer;color:'+C.sub+';font-size:20px;width:30px;height:30px;display:flex;align-items:center;justify-content:center;border-radius:8px}',
      '.lslw-back:hover{background:'+C.line+';color:'+C.text+'}',
      '.lslw-veh{flex:1;min-width:0;font-size:12px;color:'+C.sub+';white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
      '.lslw-x{cursor:pointer;color:'+C.sub+';font-size:18px;padding:4px 8px}',
      '.lslw-prog{height:3px;background:'+C.line+'}.lslw-progbar{height:100%;background:'+C.accent+';transition:width .2s}',
      '.lslw-body{padding:20px 18px;overflow-y:auto;flex:1}',
      '.lslw-q{font-size:19px;font-weight:700;color:'+C.text+';margin:0 0 4px}',
      '.lslw-hint{font-size:12px;color:'+C.sub+';margin:0 0 18px}',
      '.lslw-opt{display:block;width:100%;text-align:left;padding:15px 16px;margin:8px 0;background:'+C.bg+';border:1px solid '+C.line+';border-radius:11px;color:'+C.text+';font-size:15px;font-weight:600;cursor:pointer}',
      '.lslw-opt:hover{border-color:'+C.accent+'}.lslw-opt.sel{border-color:'+C.accent+';background:rgba(124,58,237,.12)}',
      '.lslw-opt small{display:block;font-weight:400;color:'+C.sub+';font-size:11px;margin-top:3px}',
      '.lslw-inp{width:100%;box-sizing:border-box;padding:13px 14px;background:'+C.bg+';border:1px solid '+C.line+';border-radius:10px;color:'+C.text+';font-size:16px;margin:6px 0}',
      '.lslw-inp:focus{outline:none;border-color:'+C.accent+'}',
      '.lslw-lbl{font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:'+C.sub+';margin:12px 0 2px;font-weight:700}',
      '.lslw-row{padding:11px 13px;border:1px solid '+C.line+';border-radius:9px;margin:5px 0;cursor:pointer;font-size:14px;color:'+C.text+'}',
      '.lslw-row:hover{border-color:'+C.accent+';background:rgba(124,58,237,.08)}',
      '.lslw-chip{display:inline-block;padding:8px 12px;margin:4px 6px 4px 0;border:1px solid '+C.accent+';border-radius:20px;color:'+C.accent2+';font-size:13px;cursor:pointer}',
      '.lslw-chip:hover{background:rgba(124,58,237,.15)}',
      '.lslw-foot{padding:14px 18px;border-top:1px solid '+C.line+';display:flex;gap:10px}',
      '.lslw-btn{flex:1;padding:14px;border:none;border-radius:11px;background:'+C.accent+';color:#fff;font-size:15px;font-weight:700;cursor:pointer}',
      '.lslw-btn:disabled{opacity:.4;cursor:not-allowed}',
      '.lslw-btn.ghost{background:'+C.bg+';color:'+C.sub+';border:1px solid '+C.line+'}',
      '.lslw-front{font-size:13px;font-weight:700;margin:8px 0 0}',
      '.lslw-sum{font-size:13px;color:'+C.text+';padding:9px 0;border-bottom:1px solid '+C.line+';display:flex;justify-content:space-between;gap:10px;cursor:pointer}',
      '.lslw-sum span:first-child{color:'+C.sub+'}.lslw-sum .ed{color:'+C.accent2+';font-size:11px}',
      '.lslw-toggle{display:flex;gap:8px;margin:6px 0}',
      '.lslw-toggle button{flex:1;padding:12px;border:1px solid '+C.line+';border-radius:10px;background:'+C.bg+';color:'+C.text+';font-weight:600;cursor:pointer}',
      '.lslw-toggle button.on{border-color:'+C.accent+';background:rgba(124,58,237,.15);color:'+C.accent2+'}'
    ].join('');
    document.head.appendChild(s);
  }

  function money(n){ n = Number(n)||0; return '$' + n.toLocaleString(); }
  function num(v){ if(v==null) return 0; v=String(v).replace(/[$,]/g,'').trim(); var n=parseFloat(v); return isNaN(n)?0:Math.round(n); }
  function el(tag, props){ var e=document.createElement(tag); props=props||{}; for(var k in props){ if(k==='class')e.className=props[k]; else if(k==='html')e.innerHTML=props[k]; else if(k==='text')e.textContent=props[k]; else if(k.slice(0,2)==='on')e[k.toLowerCase()]=props[k]; else e.setAttribute(k,props[k]); } for(var i=2;i<arguments.length;i++){ var c=arguments[i]; if(c==null)continue; e.appendChild(typeof c==='string'?document.createTextNode(c):c);} return e; }

  var ORDER = ['bought_from','dealer_pick','seller_form','factory_name','buy_rep','paid','title','payoff','sold_to','sell_rep','sold_for','disposition','fee_pack','fee_transport','fee_referral','fee_recon','fee_mcd','review'];
  function applies(id){
    if(id==='dealer_pick') return state.source_kind==='wholesaler';
    if(id==='seller_form') return state.source_kind==='individual';
    if(id==='factory_name') return state.source_kind==='factory';
    if(id==='payoff') return state.title_status==='PayOff';
    if(id==='sell_rep'||id==='sold_for') return !state.not_sold;
    return true;
  }
  function nextId(){ var i=ORDER.indexOf(cur); for(var j=i+1;j<ORDER.length;j++){ if(applies(ORDER[j])) return ORDER[j]; } return null; }
  function advance(){ var n=nextId(); if(n){ cur=n; hist.push(n); render(); } }
  function go(id){ cur=id; hist.push(id); render(); }
  function back(){ if(hist.length>1){ hist.pop(); cur=hist[hist.length-1]; render(); } else close(); }

  function frontNow(){ var f=state.fees; var supp=num(f.pack)+num(f.transport)+num(f.referral)+num(f.recon)+num(f.mcd); return num(state.sale_price)-num(state.purchase_cost)-supp; }

  // ---- step renderers: each returns {q, hint, body(el), footPrimary?, primaryLabel?, primaryEnabled?} ----
  var STEP = {};
  STEP.bought_from = function(){
    var b=el('div'); ['wholesaler|Dealer / wholesaler|95% of cars','individual|Individual (private party)|retail seller','factory|Factory|OEM / captive'].forEach(function(o){
      var p=o.split('|'); var opt=el('button',{class:'lslw-opt'+(state.source_kind===p[0]?' sel':''),onclick:function(){ state.source_kind=p[0]; advance(); }},p[1]);
      opt.appendChild(el('small',{text:p[2]})); b.appendChild(opt);
    });
    return {q:'Where did it come from?', hint:'Who you bought this car from', body:b};
  };
  STEP.dealer_pick = function(){
    var b=el('div');
    if(state.supplier_name){ b.appendChild(el('div',{class:'lslw-row',html:'✓ <b>'+state.supplier_name+'</b> &nbsp;<span style="color:'+C.accent2+'">change</span>',onclick:function(){ state.supplier_id=null; state.supplier_name=null; render(); }})); }
    else { autocomplete(b,{endpoint:'/api/lsl/suppliers',placeholder:'Search dealer you bought from…',onPick:function(r){ state.supplier_id=r.id; state.supplier_name=r.name; advance(); }}); }
    return {q:'Which dealer?', hint:'The seller (where you bought it)', body:b};
  };
  STEP.seller_form = function(){
    var b=el('div'); var s=state.seller=state.seller||{};
    function f(key,label,ph,type){ b.appendChild(el('div',{class:'lslw-lbl',text:label})); var i=el('input',{class:'lslw-inp',placeholder:ph||'',value:s[key]||'',type:type||'text',oninput:function(){ s[key]=i.value; }}); b.appendChild(i); }
    f('first_name','First name','Jane'); f('last_name','Last name','Doe');
    f('drivers_license','Driver’s license #','DL number');
    f('ssn_last4','Last 4 of SSN (your records)','1234');
    f('mobile','Phone','(305) 555-1234','tel'); f('email','Email','jane@email.com','email');
    f('address_street','Address','123 Main St'); f('address_city','City'); f('address_state','State');
    b.appendChild(el('div',{class:'lslw-lbl',text:'Drivers license photo (quickdrop)'}));
    var dlst=el('div',{class:'lslw-hint',text:(s.dl_photo_url?'✓ DL uploaded':'')});
    var fi=el('input',{type:'file',accept:'image/*',class:'lslw-inp'});
    fi.onchange=function(){ var file=fi.files&&fi.files[0]; if(!file)return; dlst.style.color=C.sub; dlst.textContent='Uploading...'; var fd=new FormData(); fd.append('file',file); fetch('/api/bid/'+BID+'/dl-upload',{method:'POST',body:fd}).then(function(r){return r.json();}).then(function(d){ if(d&&d.ok){ s.dl_photo_url=d.url; dlst.style.color=C.green; dlst.textContent='✓ DL uploaded'; } else { dlst.style.color=C.red; dlst.textContent='upload failed'; } }).catch(function(){ dlst.style.color=C.red; dlst.textContent='upload error'; }); };
    b.appendChild(fi); b.appendChild(dlst);
    return {q:'Seller info', hint:'The private party you bought from', body:b, primaryLabel:'Next', primaryEnabled:function(){ return (s.first_name||s.last_name); }, footPrimary:advance};
  };
  STEP.factory_name = function(){
    var b=el('div'); var i=el('input',{class:'lslw-inp',placeholder:'Factory / OEM account',value:state.factory_name||'',oninput:function(){ state.factory_name=i.value; }}); b.appendChild(i);
    return {q:'Factory source', hint:'OEM / captive name', body:b, primaryLabel:'Next', primaryEnabled:function(){return !!state.factory_name;}, footPrimary:advance};
  };
  STEP.buy_rep = function(){ return repStep('buy_rep_id','Who bought it?','The rep who sourced this car'); };
  STEP.sell_rep = function(){ return repStep('sell_rep_id','Who sold it?','The salesperson on the sale'); };
  function repStep(key,q,hint){
    var b=el('div');
    var draw=function(list){ b.innerHTML=''; list.forEach(function(r){ b.appendChild(el('div',{class:'lslw-row'+(state[key]===r.id?' sel':''),onclick:function(){ state[key]=r.id; state[key+'_name']=r.full_name; advance(); }}, r.full_name + (r.deals_12mo? ' · '+r.deals_12mo:''))); }); };
    if(reps){ draw(reps); } else { fetch('/api/lsl/sales-reps').then(function(r){return r.json();}).then(function(d){ reps=(d.results||[]); draw(reps); }); b.appendChild(el('div',{class:'lslw-hint',text:'Loading reps…'})); }
    return {q:q, hint:hint, body:b};
  }
  STEP.paid = function(){
    var b=el('div'); var i=el('input',{class:'lslw-inp',inputmode:'numeric',placeholder:'$0',value:state.purchase_cost?money(state.purchase_cost):'',oninput:function(){ state.purchase_cost=num(i.value); }});
    b.appendChild(i);
    return {q:'What did you pay?', hint:'Your purchase cost', body:b, primaryLabel:'Next', primaryEnabled:function(){return num(state.purchase_cost)>0;}, footPrimary:advance};
  };
  STEP.title = function(){
    var b=el('div'); [['Yes','Have it (clear title)'],['Pending','Title pending'],['PayOff','Lien / payoff'],['Lost','Title lost']].forEach(function(o){
      b.appendChild(el('button',{class:'lslw-opt'+(state.title_status===o[0]?' sel':''),onclick:function(){ state.title_status=o[0]; advance(); }},o[1]));
    });
    return {q:'Title?', hint:'Title status on the car', body:b};
  };
  STEP.payoff = function(){
    var b=el('div'); var po=state.payoff=state.payoff||{text_evelyn:true};
    b.appendChild(el('div',{class:'lslw-lbl',text:'Lien company'}));
    if(po.lien_company){ b.appendChild(el('div',{class:'lslw-row',html:'✓ <b>'+po.lien_company+'</b> &nbsp;<span style="color:'+C.accent2+'">change</span>',onclick:function(){ po.lien_company=null; render(); }})); }
    else {
      var LIENS=['Mercedes-Benz Financial Services','Land Rover Financial','BMW Financial Services','Capital One Auto Finance','GM Financial','Ally Financial','Lexus Financial Services','Chase Auto Finance','Nissan Motor Acceptance','Advia Credit Union','Luxury Lease Company','DCFU Financial','Centennial Lending','Acura Financial Services','Aston Martin Financial Services','Audi Financial Services','Bentley Financial Services','Ferrari Financial Services','Porsche Financial Services','Rolls-Royce Financial Services','American Honda Finance','American Credit Acceptance','Americredit','Bridgecrest','Carmax Auto Finance','Carvant Financial','Chrysler Capital','Ford Credit','Toyota Financial Services','Volkswagen Credit','Bank of America'];
      var chips=el('div'); LIENS.slice(0,6).forEach(function(n){ chips.appendChild(el('span',{class:'lslw-chip',onclick:function(){ po.lien_company=n; render(); }},n)); }); b.appendChild(chips);
      var dl=el('datalist',{id:'lslw-liens'}); LIENS.forEach(function(n){ dl.appendChild(el('option',{value:n})); }); b.appendChild(dl);
      var i=el('input',{class:'lslw-inp',list:'lslw-liens',placeholder:'search or type lien company…',oninput:function(){ po.lien_company=i.value; }}); b.appendChild(i);
    }
    var amt=el('input',{class:'lslw-inp',inputmode:'numeric',placeholder:'Payoff amount $',value:po.amount?money(po.amount):'',oninput:function(){ po.amount=num(amt.value); }});
    b.appendChild(el('div',{class:'lslw-lbl',text:'Payoff amount'})); b.appendChild(amt);
    var gu=el('input',{class:'lslw-inp',type:'date',value:po.good_until||'',oninput:function(){ po.good_until=gu.value; }});
    b.appendChild(el('div',{class:'lslw-lbl',text:'Good until'})); b.appendChild(gu);
    var acc=el('input',{class:'lslw-inp',placeholder:'Account #',value:po.account_no||'',oninput:function(){ po.account_no=acc.value; }});
    b.appendChild(el('div',{class:'lslw-lbl',text:'Account #'})); b.appendChild(acc);
    var ev=el('label',{class:'lslw-opt',style:'display:flex;align-items:center;gap:10px'}); var cb=el('input',{type:'checkbox'}); cb.checked=po.text_evelyn!==false; cb.onchange=function(){ po.text_evelyn=cb.checked; }; ev.appendChild(cb); ev.appendChild(document.createTextNode('📲 Text Evelyn the payoff details')); b.appendChild(ev);
    return {q:'Payoff details', hint:'Lien to pay off on this car', body:b, primaryLabel:'Next', primaryEnabled:function(){return !!po.lien_company && num(po.amount)>0;}, footPrimary:advance};
  };
  STEP.sold_to = function(){
    var b=el('div');
    if(state.buyer_name){ b.appendChild(el('div',{class:'lslw-row',html:'✓ <b>'+state.buyer_name+'</b> &nbsp;<span style="color:'+C.accent2+'">change</span>',onclick:function(){ state.buyer_customer_id=null; state.buyer_name=null; render(); }})); }
    else {
      var cand=(ctx.buyer_candidates||[]); if(cand.length){ var chips=el('div'); chips.appendChild(el('div',{class:'lslw-lbl',text:'Likely buyers'})); cand.forEach(function(r){ chips.appendChild(el('span',{class:'lslw-chip',onclick:function(){ state.buyer_customer_id=r.id; state.buyer_name=r.name; state.sell_kind='wholesale'; advance(); }},r.name)); }); b.appendChild(chips); }
      autocomplete(b,{endpoint:'/api/lsl/customers',placeholder:'Search buyer dealer…',onPick:function(r){ state.buyer_customer_id=r.id; state.buyer_name=r.name; state.sell_kind='wholesale'; advance(); }});
    }
    b.appendChild(el('div',{class:'lslw-row',style:'margin-top:14px;border-style:dashed;text-align:center;color:'+C.sub,onclick:function(){ state.not_sold=true; state.sell_kind=null; advance(); }},'Not sold yet — just stock it'));
    return {q:'Who bought it?', hint:'The dealer you sold to', body:b};
  };
  STEP.sold_for = function(){
    var b=el('div'); var i=el('input',{class:'lslw-inp',inputmode:'numeric',placeholder:'$0',value:state.sale_price?money(state.sale_price):(ctx.prefill.sale_hint?money(ctx.prefill.sale_hint):''),oninput:function(){ state.sale_price=num(i.value); fr.textContent='Front: '+money(frontNow()); fr.style.color=frontNow()>=0?C.green:C.red; }});
    if(!state.sale_price && ctx.prefill.sale_hint) state.sale_price=ctx.prefill.sale_hint;
    b.appendChild(i); var fr=el('div',{class:'lslw-front',text:'Front: '+money(frontNow())}); fr.style.color=frontNow()>=0?C.green:C.red; b.appendChild(fr);
    return {q:'What did it sell for?', hint:'Sale price to the buyer', body:b, primaryLabel:'Next', primaryEnabled:function(){return num(state.sale_price)>0;}, footPrimary:advance};
  };
  function feeStep(key,q,hint,prefill){
    return function(){ var b=el('div'); var i=el('input',{class:'lslw-inp',inputmode:'numeric',placeholder:'$0',value:(state.fees[key]!=null?money(state.fees[key]):(prefill?money(prefill):'')),oninput:function(){ state.fees[key]=num(i.value); }});
      if(state.fees[key]==null && prefill!=null) state.fees[key]=prefill;
      b.appendChild(i); return {q:q, hint:hint, body:b, primaryLabel:'Next', footPrimary:advance, primaryEnabled:function(){return true;}}; };
  }
  STEP.disposition = function(){
    var b=el('div');
    [['WholesaleImmediately','Wholesale immediately','flip now (most common)'],['RetailForXDays','Retail for X days','then wholesale'],['Retail','Retail','hold to retail']].forEach(function(o){
      var opt=el('button',{class:'lslw-opt'+(state.disposition_intention===o[0]?' sel':''),onclick:function(){ state.disposition_intention=o[0]; if(o[0]!=='RetailForXDays')state.retail_days=null; render(); }},o[1]);
      opt.appendChild(el('small',{text:o[2]})); b.appendChild(opt);
    });
    if(state.disposition_intention==='RetailForXDays'){
      b.appendChild(el('div',{class:'lslw-lbl',text:'Retail day limit'}));
      var t=el('div',{class:'lslw-toggle'});
      [5,10,14].forEach(function(dd){ var bb=el('button',{text:dd+' days',onclick:function(){ state.retail_days=dd; render(); }}); if(state.retail_days===dd)bb.className='on'; t.appendChild(bb); });
      b.appendChild(t);
    }
    return {q:'Plan for the car?', hint:'What you intend to do with it', body:b, primaryLabel:'Next', footPrimary:advance, primaryEnabled:function(){ return state.disposition_intention!=='RetailForXDays' || !!state.retail_days; }};
  };
  STEP.fee_pack = feeStep('pack','Inventory pack','Standard pack — usually $100',100);
  STEP.fee_transport = feeStep('transport','Transport','Set per car');
  STEP.fee_referral = function(){
    var b=el('div');
    var i=el('input',{class:'lslw-inp',inputmode:'numeric',placeholder:'$0',value:(state.fees.referral!=null?money(state.fees.referral):''),oninput:function(){ state.fees.referral=num(i.value); }});
    b.appendChild(el('div',{class:'lslw-lbl',text:'Amount ($0 if none)'})); b.appendChild(i);
    b.appendChild(el('div',{class:'lslw-lbl',text:'Referred by'}));
    var REFS=['Lucia Martin','Vlad Sidorenko','Rose Schwaller','Teya Dawson'];
    var chips=el('div'); REFS.forEach(function(n){ chips.appendChild(el('span',{class:'lslw-chip',onclick:function(){ state.fees.referral_name=n; render(); }},n)); }); b.appendChild(chips);
    var ri=el('input',{class:'lslw-inp',placeholder:'name (optional)',value:state.fees.referral_name||'',oninput:function(){ state.fees.referral_name=ri.value; }}); b.appendChild(ri);
    return {q:'Referral fee', hint:'$0 if none; name who referred it', body:b, primaryLabel:'Next', footPrimary:advance, primaryEnabled:function(){return true;}};
  };
  STEP.fee_recon = feeStep('recon','Recon','$0 if none');
  STEP.fee_mcd = function(){
    var b=el('div'); var t=el('div',{class:'lslw-toggle'});
    var no=el('button',{text:'No'}), yes=el('button',{text:'Yes ($50)'});
    function upd(){ var on=num(state.fees.mcd)===50; yes.className=on?'on':''; no.className=on?'':'on'; }
    no.onclick=function(){ state.fees.mcd=0; upd(); }; yes.onclick=function(){ state.fees.mcd=50; upd(); };
    if(state.fees.mcd==null) state.fees.mcd=0; t.appendChild(no); t.appendChild(yes); b.appendChild(t); upd();
    return {q:'MCD live fee?', hint:'Flat $50 when it applies', body:b, primaryLabel:'Review', footPrimary:advance, primaryEnabled:function(){return true;}};
  };
  STEP.review = function(){
    var b=el('div'); var f=state.fees; var supp=num(f.pack)+num(f.transport)+num(f.referral)+num(f.recon)+num(f.mcd);
    function row(label,val,step){ b.appendChild(el('div',{class:'lslw-sum',onclick:step?function(){go(step);}:null}, el('span',{text:label}), el('span',{},(val||'—')+(step?'  ':''), step?el('span',{class:'ed',text:'edit'}):document.createTextNode('')))); }
    var boughtFrom = state.source_kind==='wholesaler'?state.supplier_name : state.source_kind==='individual'?((state.seller&&((state.seller.first_name||'')+' '+(state.seller.last_name||'')).trim())||'Individual') : (state.factory_name||'Factory');
    row('Bought from', boughtFrom, state.source_kind==='wholesaler'?'dealer_pick':state.source_kind==='individual'?'seller_form':'factory_name');
    row('Buy rep', state.buy_rep_id_name, 'buy_rep');
    row('Paid', money(state.purchase_cost), 'paid');
    row('Title', state.title_status, 'title');
    row('Plan', state.disposition_intention+((state.disposition_intention==='RetailForXDays'&&state.retail_days)?(' '+state.retail_days+'d'):''), 'disposition');
    if(state.title_status==='PayOff'){ var po=state.payoff||{}; row('Payoff', (po.lien_company||'')+' '+money(po.amount)+(po.good_until?' (good '+po.good_until+')':'')+(po.text_evelyn!==false?' · texting Evelyn':''), 'payoff'); }
    if(state.not_sold){ row('Sold to', 'NOT SOLD — inventory only', 'sold_to'); }
    else {
      row('Sold to', state.buyer_name, 'sold_to');
      row('Sell rep', state.sell_rep_id_name, 'sell_rep');
      row('Sold for', money(state.sale_price), 'sold_for');
    }
    row('Fees', 'pack '+money(f.pack)+' · trans '+money(f.transport)+' · ref '+money(f.referral)+' · recon '+money(f.recon)+' · mcd '+money(f.mcd), 'fee_pack');
    if(!state.not_sold){ var front=num(state.sale_price)-num(state.purchase_cost)-supp; var fr=el('div',{class:'lslw-front',style:'margin-top:14px;font-size:16px',text:'Front: '+money(front)}); fr.style.color=front>=0?C.green:C.red; b.appendChild(fr); }
    var msg=el('div',{class:'lslw-hint',style:'margin-top:12px'}); b.appendChild(msg);
    return {q:'Review & stage', hint:'Confirm — staged locally (no live LSL write yet)', body:b, primaryLabel:'Save booking', footPrimary:function(){ doStage(msg); }, primaryEnabled:function(){return true;}};
  };

  function autocomplete(b, opt){
    var inp=el('input',{class:'lslw-inp',placeholder:opt.placeholder}); var res=el('div'); b.appendChild(inp); b.appendChild(res); var t;
    inp.oninput=function(){ clearTimeout(t); t=setTimeout(function(){ var q=inp.value.trim(); fetch(opt.endpoint+'?q='+encodeURIComponent(q)+'&limit=12').then(function(r){return r.json();}).then(function(d){ res.innerHTML=''; (d.results||[]).forEach(function(row){ res.appendChild(el('div',{class:'lslw-row',onclick:function(){ opt.onPick(row); }}, row.name + (row.deals_12mo?(' · '+row.deals_12mo+' deals'):''))); }); }); }, 200); };
    setTimeout(function(){ inp.focus(); },50);
  }

  function render(){
    var def = STEP[cur](); var step = def;
    host.querySelector('#lslw-card').innerHTML='';
    var card=host.querySelector('#lslw-card');
    var top=el('div',{class:'lslw-top'},
      el('div',{class:'lslw-back',text:'‹',onclick:back}),
      el('div',{class:'lslw-veh',text:ctx.vehicle.label+'  ·  '+(ctx.vehicle.mileage||0).toLocaleString()+' mi'}),
      el('div',{class:'lslw-x',text:'✕',onclick:close}));
    card.appendChild(top);
    var prog=el('div',{class:'lslw-prog'}); var pct=Math.round((ORDER.indexOf(cur)+1)/ORDER.length*100); prog.appendChild(el('div',{class:'lslw-progbar',style:'width:'+pct+'%'})); card.appendChild(prog);
    var body=el('div',{class:'lslw-body'}); body.appendChild(el('div',{class:'lslw-q',text:step.q})); if(step.hint)body.appendChild(el('div',{class:'lslw-hint',text:step.hint})); body.appendChild(step.body); card.appendChild(body);
    if(step.footPrimary){ var foot=el('div',{class:'lslw-foot'}); var btn=el('button',{class:'lslw-btn',text:step.primaryLabel||'Next',onclick:step.footPrimary}); if(step.primaryEnabled && !step.primaryEnabled()){ btn.disabled=true; var iv=setInterval(function(){ if(!document.body.contains(btn)){clearInterval(iv);return;} btn.disabled=!step.primaryEnabled(); },300); } foot.appendChild(btn); card.appendChild(foot); }
  }

  function doStage(msg){
    msg.style.color=C.sub; msg.textContent='Staging…';
    var f=state.fees;
    var payload={ source_kind:state.source_kind, supplier_id:state.supplier_id, factory_name:state.factory_name,
      seller:state.seller, purchase_cost:state.purchase_cost, title_status:state.title_status, payoff:state.payoff,
      not_sold:!!state.not_sold, sell_kind:state.sell_kind||'wholesale', buyer_customer_id:state.buyer_customer_id,
      sale_price:state.sale_price, buy_rep_id:state.buy_rep_id, sell_rep_id:state.sell_rep_id||state.buy_rep_id,
      disposition_intention:state.disposition_intention, retail_days:(state.retail_days||0),
      fees:{pack:num(f.pack),transport:num(f.transport),referral:num(f.referral),recon:num(f.recon),mcd:num(f.mcd),referral_name:(f.referral_name||'')} };
    fetch('/api/bid/'+BID+'/lsl-stage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
      .then(function(r){return r.json();}).then(function(d){
        if(d.ok){ msg.style.color=C.green; msg.innerHTML='✓ Saved'+(d.evelyn_texted?' · texted Evelyn'+(d.evelyn_dl_mms?' (+DL)':''):'')+'. '+(d.note||''); setTimeout(function(){ close(); location.reload(); }, 1400); }
        else { msg.style.color=C.red; msg.textContent='Error: '+(d.error||'stage failed'); }
      }).catch(function(e){ msg.style.color=C.red; msg.textContent='Network error: '+e; });
  }

  function close(){ window.__ewHoldReload=false; if(host){ host.remove(); host=null; } }

  window.lslwOpen = function(){
    if(!BID){ alert('No bid id'); return; }
    injectCss(); window.__ewHoldReload=true;
    host=el('div',{id:'lslw-ov',onclick:function(e){ if(e.target===host) close(); }}); host.appendChild(el('div',{id:'lslw-card'})); document.body.appendChild(host);
    var loading=host.querySelector('#lslw-card'); loading.appendChild(el('div',{class:'lslw-body',style:'color:'+C.sub,text:'Loading…'}));
    fetch('/api/bid/'+BID+'/book-context').then(function(r){return r.json();}).then(function(d){
      if(!d.ok){ loading.innerHTML='<div class="lslw-body" style="color:'+C.red+'">'+(d.error||'context error')+'</div>'; return; }
      ctx=d; state={ source_kind:'wholesaler', title_status:'Yes', not_sold:false, sell_kind:'wholesale', disposition_intention:'WholesaleImmediately',
        purchase_cost:(ctx.prefill.purchase_cost||0), fees:Object.assign({pack:100,transport:0,referral:0,recon:0,mcd:0}, ctx.prefill.fees||{}) };
      cur='bought_from'; hist=['bought_from']; render();
    }).catch(function(e){ loading.innerHTML='<div class="lslw-body" style="color:'+C.red+'">Network error: '+e+'</div>'; });
  };
  document.addEventListener('keydown', function(e){ if(e.key==='Escape' && host) close(); });
})();
