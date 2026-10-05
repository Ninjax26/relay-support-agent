const $ = id => document.getElementById(id);
let current = null;
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
async function history() {
  const runs = await api('/api/runs');
  $('history').replaceChildren();
  if (!runs.length) $('history').append(element('p','No investigations yet.','muted'));
  runs.forEach(run => {
    const button=element('button',run.message.slice(0,58)+(run.message.length>58?'…':''));
    button.append(element('span',`${run.status.replaceAll('_',' ')} · ${new Date(run.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`));
    button.onclick=()=>{render(run);feedback('Loaded saved investigation.');};
    $('history').append(button);
  });
}
function render(run) {
  current=run;
  $('empty-result').hidden=true; $('result').hidden=false; $('trace-section').hidden=false;
  $('result-state').textContent=run.status.replaceAll('_',' ').toUpperCase();
  $('result-meta').textContent=`${run.mode.toUpperCase()} · ${run.trace.length} TOOL CALLS · ${run.duration_ms} MS · ${run.usage.input_tokens+run.usage.output_tokens} TOKENS`;
  $('answer').textContent=run.answer.summary;
  $('action').textContent=run.answer.action.replaceAll('_',' ').replace(/^./,x=>x.toUpperCase());
  $('reason').textContent=run.answer.reason;
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
    state.append(element('p','Saved before planning and verification were added. Run a new investigation to use the updated workflow.'));
  }
  $('citations').replaceChildren();
  run.answer.citations.forEach(id=>{
    const doc=run.evidence.find(item=>item.id===id);
    const details=element('details'); details.append(element('summary',id));
    details.append(element('pre',doc.text ? `${doc.title}\n\n${doc.text}` : JSON.stringify(doc,null,2)));
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
  event.preventDefault(); $('run').disabled=true; $('run').textContent='Investigating…'; $('result-state').textContent='RUNNING';
  feedback($('mode').value==='demo' ? 'Running demo plan, evidence tools, and verification…' : 'Live model is planning and selecting tools. This can take several minutes…');
  try { render(await api('/api/runs',{message:$('message').value,account_id:$('account').value,mode:$('mode').value})); feedback('Investigation saved. Review the evidence before approving.'); await history(); }
  catch(error) { feedback(error.message,true); $('result-state').textContent=current ? current.status.replaceAll('_',' ').toUpperCase() : 'READY'; }
  finally { $('run').disabled=false; $('run').replaceChildren(document.createTextNode('Investigate issue'),element('span','↗')); }
};
document.querySelectorAll('[data-scenario]').forEach(button=>button.onclick=()=>{const [account,message]=scenarios[button.dataset.scenario];$('account').value=account;$('message').value=message;$('message').focus();feedback('Scenario loaded. Select Investigate issue to run it.');});
async function decide(decision) {
  if(!current) return;
  $('approve').disabled=true; $('reject').disabled=true;
  try { render(await api(`/api/runs/${current.id}/decision`,{decision}));await history();feedback(decision==='approve'?'Local review ticket created.':'Proposal rejected.'); }
  catch(error) { feedback(error.message,true);render(current); }
}
$('approve').onclick=()=>decide('approve');$('reject').onclick=()=>decide('reject');
$('refresh').onclick=()=>history().catch(error=>feedback(error.message,true));
$('download').onclick=()=>{if(!current)return;const url=URL.createObjectURL(new Blob([JSON.stringify(current,null,2)],{type:'application/json'}));const link=element('a');link.href=url;link.download=`relay-${current.id}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
$('workflow-nav').onclick=()=>{$('workflow').hidden=!$('workflow').hidden;$('workflow-nav').setAttribute('aria-expanded',String(!$('workflow').hidden));if(!$('workflow').hidden)$('workflow').scrollIntoView({block:'nearest'});};
$('workflow-nav').setAttribute('aria-expanded','false');$('workflow-nav').setAttribute('aria-controls','workflow');
$('workspace-nav').onclick=()=>{$('message').focus();};
$('mode').onchange=()=>{$('mode-tag').textContent=$('mode').value==='demo'?'DETERMINISTIC DEMO':'LIVE LLM';};
api('/api/config').then(config=>{if(!config.live_available)$('mode').options[1].disabled=true;}).catch(error=>feedback(error.message,true));
history().catch(error=>feedback(error.message,true));
