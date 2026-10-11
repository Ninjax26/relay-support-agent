const $ = id => document.getElementById(id);
let current = null;
let watchGeneration = 0;
let nextCursor = null;
let pendingSubmission = null;
let retrySubmission = null;
const active = run => ["queued", "running"].includes(run.status);
const scenarios = {
  billing: ['acct_demo', 'I was charged twice for the same invoice. Can you investigate and arrange a refund?'],
  outage: ['acct_demo', 'Our production API keeps returning 503 errors and timeouts. Is there an outage?'],
  login: ['acct_locked', 'My account is locked and I cannot login. Please help me get access.'],
  export: ['acct_basic', 'My data export failed. Is there an incident, or is my plan missing access?']
};
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Request failed');
  return result;
}
function feedback(text, error=false) { $('feedback').textContent=text; $('feedback').classList.toggle('error', error); }
function element(tag, text, className) { const node=document.createElement(tag); if(text!==undefined) node.textContent=text; if(className) node.className=className; return node; }
async function history(append=false) {
  const path = '/api/runs?limit=20' + (append && nextCursor ? '&before='+encodeURIComponent(nextCursor) : '');
  const page = await api(path);
  if (!append) $('history').replaceChildren();
  if (!page.items.length && !append) $('history').append(element('p','No investigations yet.','muted'));
  page.items.forEach(run => {
    const button=element('button',run.message.slice(0,58)+(run.message.length>58?'…':''));
    button.append(element('span',`${run.status.replaceAll('_',' ')} · ${new Date(run.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`));
    button.onclick=()=>watch(run.id).catch(error=>feedback(error.message,true));
    $('history').append(button);
  });
  nextCursor=page.next_cursor;
  $('more-history').hidden=!nextCursor;
}
async function watch(id) {
  const generation=++watchGeneration;
  let version=null;
  let failures=0;
  localStorage.setItem('relay-last-run',id);
  while(generation===watchGeneration) {
    try {
      const run=await api('/api/runs/'+encodeURIComponent(id));
      if(generation!==watchGeneration) return;
      failures=0;
      if(version!==run.version || version===null) { render(run); version=run.version; }
      if(!active(run)) {
        feedback(run.error || (['approved','rejected'].includes(run.status)?'Review decision saved.':'Investigation saved. Review the evidence before approving.'),!!run.error);
        await history();
        return;
      }
      feedback(run.status==='queued' ? 'Investigation queued. You can submit another issue while workers are busy.' : `Investigation running · ${run.trace.length} saved steps. You can leave and return to this run.`);
    } catch(error) {
      if(++failures>=5) { feedback('Progress connection lost. The job may still be running. Select it from history or refresh to reconnect.',true); return; }
    }
    await new Promise(resolve=>setTimeout(resolve,document.hidden?5000:1500));
  }
}
function render(run) {
  current=run;
  $('empty-result').hidden=true; $('result').hidden=false; $('trace-section').hidden=false;
  $('result-state').textContent=run.status.replaceAll('_',' ').toUpperCase();
  $('result-meta').textContent=`${run.mode.toUpperCase()} · ${run.trace.length} TOOL CALLS · ${run.duration_ms} MS · ${run.usage.input_tokens+run.usage.output_tokens} TOKENS`;
  $('run-context').textContent=`${run.account_id}${run.retry_of?' · Retry of '+run.retry_of.slice(0,8):''} — ${run.message}`;
  $('reply-heading').textContent=run.answer?'Suggested customer reply':active(run)?'Investigation progress':'Investigation stopped';
  $('answer').textContent=run.answer?.summary || run.error || (run.status==='queued' ? 'Waiting for an investigation worker.' : 'Collecting evidence. The final reply will appear after verification.');
  $('action').textContent=run.answer?.action.replaceAll('_',' ').replace(/^./,x=>x.toUpperCase()) || '';
  $('reason').textContent=run.answer?.reason || '';
  $('approve').closest('.approval').hidden=!run.answer;
  $('approve').closest('.approval').querySelector('.eyebrow').textContent=run.status==='awaiting_approval'?'HUMAN DECISION REQUIRED':'REVIEW DECISION RECORDED';
  $('retry-run').hidden=!['failed','interrupted'].includes(run.status);
  const state=$('agent-state');
  state.replaceChildren();
  if(run.plan) {
    state.append(element('h3','Investigation plan'));
    state.append(element('p',run.plan.objective));
    const steps=element('ol');
    run.plan.steps.forEach(step=>steps.append(element('li',step)));
    state.append(steps);
    if(run.plan.missing_information.length) {
      state.append(element('h3','Missing customer details'));
      run.plan.missing_information.forEach(info=>state.append(element('p',info)));
    }
    const review=element('details');
    review.append(element('summary',run.verification?.passed ? 'Mechanical verification passed · inspect checks' : 'Verification not available'));
    review.append(element('p',run.verification?.checks.join(' · ') || ''));
    review.append(element('p',run.verification?.limitation || ''));
    state.append(review);
  } else {
    state.append(element('p',active(run)?'The investigation plan will appear after the first saved step.':'No plan was recorded for this run.'));
  }
  $('citations').replaceChildren();
  (run.answer?.citations || []).forEach(id=>{
    const doc=run.evidence.find(item=>item.id===id);
    const details=element('details'); details.append(element('summary',id));
    details.append(element('pre',doc?.text ? `${doc.title}\n\n${doc.text}` : JSON.stringify(doc,null,2)));
    $('citations').append(details);
  });
  const pending=run.status==='awaiting_approval';
  $('approve').disabled=!pending; $('reject').disabled=!pending;
  $('decision-status').textContent=run.ticket_id ? `Approved · ${run.ticket_id} created locally.` : run.status==='rejected' ? 'Proposal rejected. No ticket was created.' : 'Awaiting your review. No action has been executed.';
  $('trace-count').textContent=`${run.trace.length} OBSERVABLE STEPS`;
  $('trace').replaceChildren();
  run.trace.forEach(step=>{
    const details=element('details'), summary=element('summary');
    const outcome = !step.ok ? 'ERROR' : step.result?.passed===false ? 'REVISE' : 'OK';
    summary.append(element('span',String(step.step).padStart(2,'0'),'step'),element('span',step.tool),element('span',`${outcome} · ${step.duration_ms} ms`,'duration'));
    details.append(summary,element('pre',JSON.stringify({arguments:step.arguments,observation:step.result},null,2)));
    $('trace').append(details);
  });
}
$('investigate-form').onsubmit=async event=>{
  event.preventDefault(); $('run').disabled=true; $('run').textContent='Submitting…';
  const request={message:$('message').value,account_id:$('account').value,mode:$('mode').value};
  const fingerprint=JSON.stringify(request);
  if(!pendingSubmission || pendingSubmission.fingerprint!==fingerprint) pendingSubmission={fingerprint,id:crypto.randomUUID()};
  try {
    const run=await api('/api/runs',{...request,request_id:pendingSubmission.id});
    pendingSubmission=null;
    render(run);
    await history();
    watch(run.id).catch(error=>feedback(error.message,true));
  } catch(error) { feedback(error.message,true); }
  finally { $('run').disabled=false; $('run').replaceChildren(document.createTextNode('Investigate issue'),element('span','↗')); }
};
$('retry-run').onclick=async()=>{
  if(!current) return;
  if(!retrySubmission || retrySubmission.runId!==current.id)retrySubmission={runId:current.id,id:crypto.randomUUID()};
  $('retry-run').disabled=true;
  try {
    const run=await api(`/api/runs/${current.id}/retry`,{request_id:retrySubmission.id});
    retrySubmission=null;
    render(run); await history(); watch(run.id).catch(error=>feedback(error.message,true));
  } catch(error) { feedback(error.message,true); }
  finally { $('retry-run').disabled=false; }
};
$('more-history').onclick=()=>history(true).catch(error=>feedback(error.message,true));
document.querySelectorAll('[data-scenario]').forEach(button=>button.onclick=()=>{const [account,message]=scenarios[button.dataset.scenario];$('account').value=account;$('message').value=message;$('message').focus();feedback('Scenario loaded. Select Investigate issue to run it.');});
async function decide(decision) {
  if(!current) return;
  $('approve').disabled=true; $('reject').disabled=true;
  try { render(await api(`/api/runs/${current.id}/decision`,{decision}));await history();feedback(decision==='approve'?'Local review ticket created.':'Proposal rejected.'); }
  catch(error) { feedback(error.message,true);render(current); }
}
$('approve').onclick=()=>decide('approve');$('reject').onclick=()=>decide('reject');
$('refresh').onclick=()=>{history().catch(error=>feedback(error.message,true));if(current)watch(current.id).catch(error=>feedback(error.message,true));};
$('download').onclick=()=>{if(!current)return;const url=URL.createObjectURL(new Blob([JSON.stringify(current,null,2)],{type:'application/json'}));const link=element('a');link.href=url;link.download=`relay-${current.id}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
$('workflow-nav').onclick=()=>{$('workflow').hidden=!$('workflow').hidden;$('workflow-nav').setAttribute('aria-expanded',String(!$('workflow').hidden));if(!$('workflow').hidden)$('workflow').scrollIntoView({block:'nearest'});};
$('workflow-nav').setAttribute('aria-expanded','false');$('workflow-nav').setAttribute('aria-controls','workflow');
$('workspace-nav').onclick=()=>{$('message').focus();};
$('mode').onchange=()=>{$('mode-tag').textContent=$('mode').value==='demo'?'DETERMINISTIC DEMO':'LIVE LLM';};
api('/api/config').then(config=>{if(!config.live_available)$('mode').options[1].disabled=true;}).catch(error=>feedback(error.message,true));
history().catch(error=>feedback(error.message,true));

const lastRun=localStorage.getItem("relay-last-run");
if(lastRun)watch(lastRun).catch(error=>feedback(error.message,true));
