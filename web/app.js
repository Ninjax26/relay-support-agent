const $ = id => document.getElementById(id);
let current = null;
let watchGeneration = 0;
let nextCursor = null;
let pendingSubmission = null;
let retrySubmission = null;
let resumeSubmission = null;
let activeStream = null;
let lastRunKey = "relay-last-run";
let registering = false;
let requestWorker = false;
const activeRequests = new Map();
const active = run => ["queued", "running"].includes(run.status);
const delivering = run => ["pending", "sending"].includes(run.ticket_delivery?.state);
const scenarios = {
  billing: ['acct_demo', 'I was charged twice for the same invoice. Can you investigate and arrange a refund?'],
  outage: ['acct_demo', 'Our production API keeps returning 503 errors and timeouts. Is there an outage?'],
  login: ['acct_locked', 'My account is locked and I cannot login. Please help me get access.'],
  export: ['acct_basic', 'My data export failed. Is there an incident, or is my plan missing access?']
};
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith('/api/session/') && !$('app').hidden) location.reload();
    throw new Error(result.error || 'Request failed');
  }
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
function processRun(run) {
  if (!requestWorker || !active(run) || activeRequests.has(run.id)) return;
  if (run.status === 'running' && Date.now() - Date.parse(run.updated_at) < 100000) return;
  const path = '/api/runs/' + encodeURIComponent(run.id) + '/process';
  activeRequests.set(run.id, api(path, {}).catch(() => {
    // A later poll can reconnect or pick up a queued job.
  }).finally(() => activeRequests.delete(run.id)));
}
async function pollWatch(id) {
  const generation=++watchGeneration;
  let version=null;
  let failures=0;
  localStorage.setItem(lastRunKey,id);
  while(generation===watchGeneration) {
    try {
      const run=await api('/api/runs/'+encodeURIComponent(id));
      if(generation!==watchGeneration) return;
      failures=0;
      processRun(run);
      if(version!==run.version || version===null) { render(run); version=run.version; }
      if(!active(run) && !delivering(run)) {
        feedback(run.status==='cancelled'?'Investigation cancelled. No review ticket was created.':run.error || (run.ticket_delivery?.state==='failed'?'Ticket delivery failed. Approval is saved; retry delivery below.':['approved','rejected'].includes(run.status)?'Review decision saved.':'Investigation saved. Review the evidence before approving.'),!!run.error || run.ticket_delivery?.state==='failed');
        await history();
        return;
      }
      if(delivering(run)) { feedback('Approval saved. Delivering the review ticket to the local test service…'); }
      else feedback(run.status==='queued' ? 'Investigation queued. You can submit another issue while workers are busy.' : `Investigation running · ${run.trace.length} saved steps. You can leave and return to this run.`);
    } catch(error) {
      if(++failures>=5) { feedback('Progress connection lost. The job may still be running. Select it from history or refresh to reconnect.',true); return; }
    }
    await new Promise(resolve=>setTimeout(resolve,document.hidden?5000:1500));
  }
}
function watch(id) {
  if (activeStream) { activeStream.close(); activeStream=null; }
  if (requestWorker || !window.EventSource) return pollWatch(id);
  const generation=++watchGeneration;
  localStorage.setItem(lastRunKey,id);
  const source=new EventSource('/api/runs/'+encodeURIComponent(id)+'/events');
  activeStream=source;
  source.onmessage=event=>{
    if (generation!==watchGeneration) { source.close(); return; }
    const run=JSON.parse(event.data);
    render(run);
    if (run.cancel_requested && run.status==='running') feedback('Cancellation requested. Relay will stop after the current model request finishes.');
    else if (active(run)) feedback(run.status==='queued'?'Investigation queued.':'Investigation running · '+run.trace.length+' saved steps.');
    else if (delivering(run)) feedback('Approval saved. Delivering the ticket to the local test service…');
    else {
      source.close(); activeStream=null;
      feedback(run.status==='cancelled'?'Investigation cancelled. No review ticket was created.':run.error || (run.ticket_delivery?.state==='failed'?'Ticket delivery failed. Approval is saved.':'Investigation saved. Review the evidence before approving.'),!!run.error || run.ticket_delivery?.state==='failed');
      history().catch(error=>feedback(error.message,true));
    }
  };
  source.onerror=()=>{
    source.close(); activeStream=null;
    if(generation===watchGeneration) pollWatch(id).catch(error=>feedback(error.message,true));
  };
}
function render(run) {
  current=run;
  $('empty-result').hidden=true; $('result').hidden=false; $('trace-section').hidden=false;
  $('result-state').textContent=run.status.replaceAll('_',' ').toUpperCase();
  $('result-meta').textContent=`${run.mode.toUpperCase()} · ${run.trace.length} TOOL CALLS · ${run.duration_ms} MS · ${run.usage.input_tokens+run.usage.output_tokens} TOKENS`;
  $('run-context').textContent=`${run.account_id}${run.policy_count ? ' · ' + run.policy_count + ' workspace ' + (run.policy_count === 1 ? 'policy' : 'policies') : ''}${run.resume_count?' · Resumed '+run.resume_count+' time(s)':''}${run.retry_of?' · Retry of '+run.retry_of.slice(0,8):''} — ${run.message}`;
  $('reply-heading').textContent=run.answer?'Suggested customer reply':active(run)?'Investigation progress':'Investigation stopped';
  $('answer').textContent=run.answer?.summary || run.error || (run.status==='queued' ? 'Waiting for an investigation worker.' : 'Collecting evidence. The final reply will appear after verification.');
  $('action').textContent=run.answer?.action.replaceAll('_',' ').replace(/^./,x=>x.toUpperCase()) || '';
  $('reason').textContent=run.answer?.reason || '';
  $('approve').closest('.approval').hidden=!run.answer;
  $('approve').closest('.approval').querySelector('.eyebrow').textContent=run.status==='awaiting_approval'?'HUMAN DECISION REQUIRED':'REVIEW DECISION RECORDED';
  $('retry-run').hidden=!['failed','interrupted'].includes(run.status);
  $('cancel-run').hidden=!['queued','running','interrupted'].includes(run.status) || !!run.cancel_requested;
  $('resume-run').hidden=!(['failed','interrupted'].includes(run.status) && run.resume_available);
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
    review.append(element('summary',run.verification?.passed ? 'Evidence checks passed · review required' : run.verification ? 'Draft needs correction · inspect feedback' : 'Verification not available'));
    review.append(element('p',run.verification?.checks.join(' · ') || ''));
    (run.verification?.issues || []).forEach(issue=>review.append(element('p',issue)));
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
    if (id.startsWith('POL-') && doc?.title) details.querySelector('summary').textContent = 'Workspace policy · ' + doc.title;
    $('citations').append(details);
  });
  const pending=run.status==='awaiting_approval';
  $('approve').disabled=!pending; $('reject').disabled=!pending;
  const delivery=run.ticket_delivery;
  $('retry-ticket').hidden=delivery?.state!=='failed';
  const deliveryText=delivery?.state==='delivered' ? `Delivered to test service · ${delivery.external_id}.` :
    delivery?.state==='failed' ? `Test ticket delivery failed (${delivery.error_code}). Approval is saved; retry delivery below.` :
    delivery ? 'Approval saved · test ticket delivery pending.' : '';
  $('decision-status').textContent=run.ticket_id ? `${run.ticket_id} · ${deliveryText || 'Local review ticket created.'}` : run.status==='rejected' ? 'Proposal rejected. No ticket was created.' : 'Awaiting your review. No action has been executed.';
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
  if (!$('policy-choice').hidden && $('use-policies').checked) request.use_policies = true;
  const fingerprint=JSON.stringify(request);
  if(!pendingSubmission || pendingSubmission.fingerprint!==fingerprint) pendingSubmission={fingerprint,id:crypto.randomUUID()};
  try {
    const run=await api('/api/runs',{...request,request_id:pendingSubmission.id});
    pendingSubmission=null;
    processRun(run);
    render(run);
    await history();
    watch(run.id).catch(error=>feedback(error.message,true));
  } catch(error) { feedback(error.message,true); }
  finally { $('run').disabled=false; $('run').replaceChildren(document.createTextNode('Investigate issue'),element('span','↗')); }
};
$('cancel-run').onclick=async()=>{
  if(!current) return;
  $('cancel-run').disabled=true;
  try { const run=await api(`/api/runs/${current.id}/cancel`,{});render(run);watch(run.id); }
  catch(error) { feedback(error.message,true); }
  finally { $('cancel-run').disabled=false; }
};
$('resume-run').onclick=async()=>{
  if(!current) return;
  if(!resumeSubmission || resumeSubmission.runId!==current.id)resumeSubmission={runId:current.id,id:crypto.randomUUID()};
  $('resume-run').disabled=true;
  try {
    const run=await api(`/api/runs/${current.id}/resume`,{request_id:resumeSubmission.id});
    resumeSubmission=null;processRun(run);render(run);await history();watch(run.id).catch(error=>feedback(error.message,true));
  } catch(error) { feedback(error.message,true); }
  finally { $('resume-run').disabled=false; }
};
$('retry-run').onclick=async()=>{
  if(!current) return;
  if(!retrySubmission || retrySubmission.runId!==current.id)retrySubmission={runId:current.id,id:crypto.randomUUID()};
  $('retry-run').disabled=true;
  try {
    const run=await api(`/api/runs/${current.id}/retry`,{request_id:retrySubmission.id});
    retrySubmission=null;
    processRun(run);
    render(run); await history(); watch(run.id).catch(error=>feedback(error.message,true));
  } catch(error) { feedback(error.message,true); }
  finally { $('retry-run').disabled=false; }
};
$('more-history').onclick=()=>history(true).catch(error=>feedback(error.message,true));
document.querySelectorAll('[data-scenario]').forEach(button=>button.onclick=()=>{const [account,message]=scenarios[button.dataset.scenario];$('account').value=account;$('message').value=message;$('message').focus();feedback('Scenario loaded. Select Investigate issue to run it.');});
async function decide(decision) {
  if(!current) return;
  $('approve').disabled=true; $('reject').disabled=true;
  try { const run=await api(`/api/runs/${current.id}/decision`,{decision});render(run);await history();feedback(decision==='approve'?'Approval saved.':'Proposal rejected.');if(delivering(run))watch(run.id).catch(error=>feedback(error.message,true)); }
  catch(error) { feedback(error.message,true);render(current); }
}
$('retry-ticket').onclick=async()=>{
  if(!current) return;
  $('retry-ticket').disabled=true;
  try { const run=await api(`/api/runs/${current.id}/ticket/retry`,{});render(run);watch(run.id).catch(error=>feedback(error.message,true)); }
  catch(error) { feedback(error.message,true); }
  finally { $('retry-ticket').disabled=false; }
};
$('approve').onclick=()=>decide('approve');$('reject').onclick=()=>decide('reject');
$('refresh').onclick=()=>{history().catch(error=>feedback(error.message,true));if(current)watch(current.id).catch(error=>feedback(error.message,true));};
$('download').onclick=()=>{if(!current)return;const url=URL.createObjectURL(new Blob([JSON.stringify(current,null,2)],{type:'application/json'}));const link=element('a');link.href=url;link.download=`relay-${current.id}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
$('workflow-nav').onclick=()=>{$('workflow').hidden=!$('workflow').hidden;$('workflow-nav').setAttribute('aria-expanded',String(!$('workflow').hidden));if(!$('workflow').hidden)$('workflow').scrollIntoView({block:'nearest'});};
$('workflow-nav').setAttribute('aria-expanded','false');$('workflow-nav').setAttribute('aria-controls','workflow');
$('workspace-nav').onclick=()=>{$('message').focus();};
$('mode').onchange=()=>{$('mode-tag').textContent=$('mode').value==='demo'?'DETERMINISTIC DEMO':'LIVE LLM';};
async function loadPolicies() {
  const {items} = await api('/api/policies');
  $('policy-count').textContent = items.length ? '(' + items.length + ')' : '';
  $('policy-choice').hidden = !items.length;
  $('policies').replaceChildren();
  for (const policy of items) {
    const card = element('details', undefined, 'policy-entry');
    const remove = element('button', 'Remove policy', 'secondary');
    remove.type = 'button';
    remove.onclick = async () => {
      remove.disabled = true;
      try {
        await api('/api/policies/remove', {id:policy.id});
        await loadPolicies();
        $('policy-status').textContent = 'Removed from future investigations. Saved cases keep their original evidence.';
      } catch (error) {
        $('policy-status').textContent = error.message;
        remove.disabled = false;
      }
    };
    card.append(element('summary', policy.title), element('pre', policy.text), remove);
    $('policies').append(card);
  }
}
$('policy-file').onchange = async () => {
  const file = $('policy-file').files[0];
  $('policy-status').textContent = '';
  if (!file) return;
  try {
    if (!/\.(txt|md|markdown)$/i.test(file.name) || file.size > 12000) {
      throw new Error('Choose a .txt or .md file with up to 3,000 characters.');
    }
    const body = new TextDecoder('utf-8', {fatal:true}).decode(await file.arrayBuffer()).trim();
    if (!body || body.length > 3000 || body.includes('\0')) {
      throw new Error('Choose a UTF-8 policy with 1–3,000 characters.');
    }
    $('policy-title').value = file.name.replace(/\.[^.]+$/, '').slice(0,120);
    $('policy-text').value = body;
  } catch (error) {
    $('policy-status').textContent = error.message;
    $('policy-file').value = '';
  }
};
$('policy-form').onsubmit = async event => {
  event.preventDefault();
  $('save-policy').disabled = true;
  $('policy-status').textContent = '';
  try {
    await api('/api/policies', {title:$('policy-title').value, text:$('policy-text').value});
    event.target.reset();
    await loadPolicies();
    $('policy-status').textContent = 'Policy saved.';
  } catch (error) {
    $('policy-status').textContent = error.message;
  } finally {
    $('save-policy').disabled = false;
  }
};
$('google-login').onclick = async () => {
  $('google-login').disabled = true;
  $('auth-status').textContent = '';
  try {
    const result = await api('/api/session/google/start', {});
    location.assign(result.url);
  } catch (error) {
    $('auth-status').textContent = error.message;
    $('google-login').disabled = false;
  }
};
$('auth-toggle').onclick = () => {
  registering = !registering;
  $('auth-title').textContent = registering ? 'Create your Relay account' : 'Sign in to Relay';
  $('auth-submit').textContent = registering ? 'Create account' : 'Sign in';
  $('auth-toggle').textContent = registering ? 'Back to sign in' : 'Create an account';
  $('password').autocomplete = registering ? 'new-password' : 'current-password';
  $('auth-hint').hidden = !registering;
  $('username-hint').hidden = !registering;
  $('auth-status').textContent = '';
};
$('auth-form').onsubmit = async event => {
  event.preventDefault();
  $('auth-submit').disabled = true;
  $('auth-toggle').disabled = true;
  $('auth-status').textContent = '';
  try {
    await api(registering ? '/api/session/register' : '/api/session/login', {
      username: $('username').value,
      password: $('password').value
    });
    $('password').value = '';
    location.reload();
  } catch (error) {
    $('auth-status').textContent = error.message;
    $('auth-submit').disabled = false;
    $('auth-toggle').disabled = false;
  }
};
$('sign-out').onclick = async () => {
  $('sign-out').disabled = true;
  try {
    await api('/api/session/logout', {});
    location.reload();
  } catch (error) {
    feedback(error.message, true);
    $('sign-out').disabled = false;
  }
};
async function initialize() {
  const session = await api('/api/session');
  const status = new URL(location.href).searchParams.get('signin');
  if (status === 'google-failed' || status === 'google-cancelled') {
    window.history.replaceState(null, '', '/');
    $('auth-status').textContent = status === 'google-failed'
      ? 'Google sign-in could not finish. Try again or sign in with a Relay account.'
      : 'Google sign-in was cancelled.';
  }
  if (session.required && !session.user) {
    $('google-auth').hidden = !session.google_login;
    $('auth').hidden = false;
    return;
  }
  $('app').hidden = false;
  if (session.user) {
    lastRunKey = 'relay-last-run:' + session.user.id;
    $('workspace-label').textContent = session.user.username;
    $('sign-out').hidden = false;
    $('policy-library').hidden = false;
    loadPolicies().catch(error => {$('policy-status').textContent = error.message;});
  }
  const config = await api('/api/config');
  requestWorker = config.request_worker;
  $('mode').options[1].disabled = !config.live_available;
  if (session.user) $('mode').options[1].textContent = config.live_available ? 'Live AI · ' + (config.provider === 'gemini' ? 'Gemini' : 'OpenAI') : 'Live AI · unavailable';
  if (config.ticket_connector === 'sandbox') {
    $('approval-help').textContent = 'Approval creates a review ticket and sends the approved proposal to the local test ticket service.';
  }
  await history();
  const lastRun = localStorage.getItem(lastRunKey);
  if (lastRun) watch(lastRun).catch(error => feedback(error.message, true));
}
window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); });
initialize().catch(error => {
  if ($('app').hidden) {
    $('auth').hidden = false;
    $('auth-status').textContent = error.message;
  } else {
    feedback(error.message, true);
  }
});
