const $ = s => document.querySelector(s);
const monthEl = $('#month'), board = $('#leaderboard'), loading = $('#loading'), empty = $('#empty'), count = $('#count'), label = $('#monthLabel');
const tg = window.Telegram?.WebApp;
// The official SDK also exists in normal browsers (platform 'unknown', empty initData);
// only treat Telegram-specific signals (colorScheme, header colours) as authoritative inside Telegram.
const inTelegram = !!(tg && ((tg.platform && tg.platform !== 'unknown') || tg.initData));
if (tg) {
  tg.ready(); tg.expand();
  // Prevent accidental Mini App collapse while scrolling the card modal (Bot API 7.7+).
  try { if (tg.isVersionAtLeast?.('7.7')) tg.disableVerticalSwipes?.(); } catch (_) {}
}

function esc(s){return String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]))}
function medal(i){return i===1?'🥇':i===2?'🥈':i===3?'🥉':i}
// Same-origin Telegram photo proxy. Also used inside the Activity Card so html2canvas can
// include the image in the exported PNG/JPEG (a cross-origin photo_url would be omitted).
function avatar(m){return `/avatar/${m.user_id}`}
function initDataHeaders(){return tg?.initData?{'X-Telegram-Init-Data':tg.initData}: {}}
function notify(msg){ if (tg?.showAlert) { try { tg.showAlert(msg); return; } catch (_) {} } alert(msg); }

// Inline copy of the server-side placeholder so a failed avatar never shows a broken-image icon.
const AVATAR_FALLBACK = 'data:image/svg+xml;utf8,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96"><rect width="96" height="96" rx="48" fill="#252b3a"/><circle cx="48" cy="38" r="17" fill="#8b93a7"/><path d="M19 80c4-17 15-26 29-26s25 9 29 26" fill="#8b93a7"/></svg>');
function applyAvatarFallback(img){ if (img && img.dataset.fallback !== '1') { img.dataset.fallback = '1'; img.src = AVATAR_FALLBACK; } }
// Delegated (capture-phase) error handler: works for rows added later and needs no inline handlers.
board.addEventListener('error', e => { if (e.target?.classList?.contains('row-avatar')) applyAvatarFallback(e.target); }, true);
$('#cardAvatar').addEventListener('error', e => applyAvatarFallback(e.target));

let currentMember = null;
let currentMedia = {};          // {png: {...}, jpg: {...}} uploaded media per format for the open card
let loadGeneration = 0;         // increments on every load(); stale responses are ignored
let loadController = null;
const WAKE_HINT_MS = 5000, RETRY_DELAY_MS = 4000, MAX_RETRIES = 1;

function setLoadingText(waking){
  loading.classList.toggle('waking', !!waking);
  loading.textContent = '';
  if (waking) {
    loading.append('Server is waking up… ⏳');
    const s = document.createElement('small');
    s.textContent = 'Free hosting-এ প্রথম load-এ ৩০–৬০ সেকেন্ড লাগতে পারে। একটু অপেক্ষা করুন।';
    loading.append(s);
  } else {
    loading.textContent = 'Loading leaderboard…';
  }
}

function showEmpty(text){ empty.textContent = text; empty.classList.remove('hidden'); }

async function load(){
  const month = monthEl.value;
  const generation = ++loadGeneration;
  if (loadController) loadController.abort();
  loadController = new AbortController();
  const signal = loadController.signal;
  setLoadingText(false); loading.classList.remove('hidden'); empty.classList.add('hidden'); board.innerHTML = '';
  count.textContent = '0 members'; label.textContent = month || '';
  const wakeTimer = setTimeout(() => { if (generation === loadGeneration) setLoadingText(true); }, WAKE_HINT_MS);

  let attempt = 0;
  while (true) {
    try {
      const r = await fetch(`/api/leaderboard?month=${encodeURIComponent(month)}`, { signal, cache: 'no-store' });
      if (generation !== loadGeneration) return;
      let d = null;
      try { d = await r.json(); } catch (_) { d = null; }
      if (generation !== loadGeneration) return;
      if (!d) {
        // Non-JSON body (e.g. a hosting 502/503 page while the server wakes up).
        if (r.status >= 500 && attempt < MAX_RETRIES) { attempt++; await sleep(RETRY_DELAY_MS, signal); continue; }
        throw new Error('bad response');
      }
      clearTimeout(wakeTimer); loading.classList.add('hidden');
      if (!d.ok) { showEmpty(d.error || 'Could not load leaderboard.'); return; }
      label.textContent = d.month; count.textContent = `${d.count} eligible members`;
      if (!d.members.length) { showEmpty('No eligible members for this month.'); return; }
      board.innerHTML = d.members.map(m => `<div class="row ${m.rank<=3?'top':''}"><div class="pos">${medal(m.rank)}</div><img class="row-avatar" src="${avatar(m)}" alt="" loading="eager"><div class="person"><div class="name">${esc(m.first_name)}</div><div class="handle">Eligible member</div></div><div class="score">${Number(m.score).toFixed(2)}<small>/100</small></div></div>`).join('');
      return;
    } catch (e) {
      if (signal.aborted || generation !== loadGeneration) return;
      if (attempt < MAX_RETRIES) { attempt++; try { await sleep(RETRY_DELAY_MS, signal); } catch (_) { return; } continue; }
      clearTimeout(wakeTimer); loading.classList.add('hidden');
      showEmpty('Could not load leaderboard. Please refresh in a moment.');
      return;
    }
  }
}
function sleep(ms, signal){ return new Promise((res, rej) => { const t = setTimeout(res, ms); signal?.addEventListener('abort', () => { clearTimeout(t); rej(new Error('aborted')); }, { once: true }); }); }

const ADMIN_COMMENTS=[
'👑 আপনি Admin! আপনার আবার Activity Score কীসের? আপনি তো activity-র হিসাব রাখেন! 😎','😂 আপনি হিসাব রাখেন সবার, আপনার হিসাব রাখবে কে?','🫡 Admin সাহেব, নিজের rank নিয়ে এত চিন্তা কেন? Group সামলান!','🏆 আপনার Rank: Admin Supreme! এই leaderboard-এ সেই rank-এর জায়গা নেই।','📢 আপনি Admin, আপনার activity গোপনীয়… অন্তত এই বটের কাছে! 🤫','🤣 আপনি /myrank দিয়েছেন কেন? নিজের কাছে নিজের রিপোর্ট জমা দেবেন নাকি?','👀 Admin হয়েও নিজের activity দেখতে চান? সন্দেহজনক ব্যাপার!','☕ আগে চা খান Admin সাহেব, Activity Score দিয়ে কী করবেন?','🫵 আপনি তো নিয়ম বানান! নিজের জন্য আবার নিয়মের দরকার কী?','🚨 সতর্কবার্তা: Admin-এর অতিরিক্ত rank-checking শনাক্ত করা হয়েছে!','🤖 আমার database-এ আপনার rank নেই, কারণ আপনাকে হিসাবের বাইরে রাখা হয়েছে!','😎 আপনি leaderboard দেখেন, leaderboard আপনাকে দেখে না!','📊 আপনার Activity Report: Admin হওয়াটাই আপনার সবচেয়ে বড় activity!','😂 আপনি কি নিজেকেও group থেকে ban করে activity বাড়াতে চান?','👑 Admin-এর rank জানতে হলে আগে Bot-এর permission নিতে হবে!','🫡 আপনার কাজ member-দের active রাখা, নিজের score দেখে active হওয়া নয়!','🤔 আপনি Admin, নাকি নিজের fan club-এর president?','📢 এই command সাধারণ সদস্যদের জন্য। Admin-দের জন্য আছে শুধু দায়িত্ব আর দুশ্চিন্তা!','💀 আবার /myrank? Admin সাহেব, আপনার কি leaderboard-এর সঙ্গে personal শত্রুতা আছে?','🎖️ অভিনন্দন! আপনি আজও Admin পদে বহাল আছেন। এর চেয়ে বড় achievement আর কী!'
];
function adminComment(){let i=Number(localStorage.getItem('odvut-admin-comment-index')||0);const text=ADMIN_COMMENTS[i%ADMIN_COMMENTS.length];localStorage.setItem('odvut-admin-comment-index',String((i+1)%ADMIN_COMMENTS.length));return text}

let lastFocus = null;
function openActivityCard(m){
  currentMember = m;
  currentMedia = {};
  const cardImg=$('#cardAvatar');
  cardImg.dataset.fallback='';
  cardImg.crossOrigin='anonymous';
  cardImg.src=avatar(m);
  $('#cardName').textContent=m.first_name||'Member';
  $('#cardHandle').textContent=m.username?`@${m.username}`:'ODVUT INFO member';
  const admin=m.is_admin===true;
  if(admin){
    $('#cardScore').textContent='ADMIN'; $('#cardRank').textContent='∞'; $('#cardDays').textContent='—'; $('#cardTime').textContent='—'; $('#cardMessages').textContent='—';
    $('#cardStatus').textContent=adminComment(); $('#cardStatus').classList.add('admin-comment');
  } else {
    $('#cardScore').textContent=Number(m.score).toFixed(2); $('#cardRank').textContent=m.rank?`#${m.rank}`:'—'; $('#cardDays').textContent=m.active_days; $('#cardTime').textContent=m.estimated_time; $('#cardMessages').textContent=m.message_count;
    $('#cardStatus').textContent=m.eligible?'✓ LEADERBOARD ELIGIBLE':'NOT ELIGIBLE • 5 ACTIVE DAYS REQUIRED'; $('#cardStatus').classList.remove('admin-comment');
  }
  setShareStatus('');
  // The trigger button is disabled while loading (which blurs it), so fall back to it explicitly.
  const active = document.activeElement;
  lastFocus = (active && active !== document.body) ? active : $('#getMyCard');
  const modal = $('#cardModal');
  modal.classList.remove('hidden'); modal.setAttribute('aria-hidden','false');
  document.body.style.overflow = 'hidden';
  requestAnimationFrame(() => $('#closeCard').focus({ preventScroll: true }));
}

async function getMyActivityCard(){
  const btn=$('#getMyCard'); btn.disabled=true; btn.textContent='Loading…';
  try{
    if(!tg?.initData) throw new Error('এই অপশনটি Telegram Mini App-এর ভেতর থেকে ব্যবহার করুন।');
    const r=await fetch(`/api/me?month=${encodeURIComponent(monthEl.value)}`,{headers:initDataHeaders(),cache:'no-store'});
    let d=null; try{ d=await r.json(); }catch(_){ d=null; }
    if(!d) throw new Error(r.status>=500?'Server সাময়িকভাবে unavailable। একটু পরে আবার চেষ্টা করুন।':'Activity card পাওয়া যায়নি।');
    if(!r.ok||!d.ok) throw new Error(d.error||'Activity card পাওয়া যায়নি।');
    openActivityCard(d.member);
  }catch(e){notify(e.message)}
  finally{btn.disabled=false;btn.textContent='🎴 Get Your Activity Card'}
}

// ---------------------------------------------------------------------------
// Card export: fixed 540x720 CSS px stage rendered at 2x => 1080x1440 on every device.
// ---------------------------------------------------------------------------
const EXPORT_W = 540, EXPORT_H = 720, EXPORT_SCALE = 2, JPEG_MAX_BYTES = 5 * 1024 * 1024 - 64 * 1024;
const JPEG_BG = '#0b1020';

function canvasToBlob(canvas, type, quality){return new Promise((resolve,reject)=>canvas.toBlob(b=>b?resolve(b):reject(new Error('Image তৈরি করা যায়নি।')),type,quality));}

function waitForImage(img){
  if(!img) return Promise.resolve();
  const decode = () => (img.decode ? img.decode().catch(()=>{}) : Promise.resolve());
  if(img.complete) return decode();
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Profile photo is still loading. Please try again.')),8000);
    img.addEventListener('load',()=>{clearTimeout(timer);resolve()}, {once:true});
    img.addEventListener('error',()=>{clearTimeout(timer);resolve()}, {once:true}); // fallback image is applied by the error handler
  }).then(decode);
}

async function renderCardCanvas(format){
  if(!window.html2canvas) throw new Error('Card renderer is still loading. Please try again.');
  const src=$('#activityCard');
  await waitForImage($('#cardAvatar'));
  const stage=$('#exportStage');
  stage.innerHTML='';
  const clone=src.cloneNode(true);
  clone.removeAttribute('id');
  if(format==='jpeg') clone.classList.add('export-jpeg');
  const cloneImg=clone.querySelector('img');
  if(cloneImg){ cloneImg.removeAttribute('id'); cloneImg.crossOrigin='anonymous'; cloneImg.src=$('#cardAvatar').currentSrc||$('#cardAvatar').src; }
  stage.appendChild(clone);
  stage.classList.add('exporting');
  try{
    await waitForImage(cloneImg);
    return await html2canvas(clone,{
      scale:EXPORT_SCALE,width:EXPORT_W,height:EXPORT_H,windowWidth:1200,windowHeight:1000,
      useCORS:true,allowTaint:false,logging:false,imageTimeout:10000,
      backgroundColor:format==='jpeg'?JPEG_BG:null,
      onclone:(doc)=>{ const s=doc.getElementById('exportStage'); if(s){ s.style.left='0px'; s.style.top='0px'; } }
    });
  } finally {
    stage.classList.remove('exporting');
    stage.innerHTML='';
  }
}

async function renderCardBlob(format='png'){
  const canvas=await renderCardCanvas(format);
  if(!canvas || !canvas.width) throw new Error('Card render failed. Please try again.');
  if(format!=='jpeg') return {canvas,blob:await canvasToBlob(canvas,'image/png',1)};
  // Telegram photo sharing requires JPEG under 5 MB: step the quality down if needed.
  let blob=null;
  for(const q of [0.92,0.85,0.78,0.7,0.6]){
    blob=await canvasToBlob(canvas,'image/jpeg',q);
    if(blob.size<=JPEG_MAX_BYTES) break;
  }
  if(!blob || blob.size>JPEG_MAX_BYTES) throw new Error('Card image Telegram-এর 5 MB limit-এর মধ্যে আনা যায়নি।');
  return {canvas,blob};
}

function safeFilename(ext='png'){return `odvut-info-activity-card-${($('#cardName').textContent||'member').replace(/[^a-z0-9_-]+/gi,'-').replace(/^-+|-+$/g,'')||'member'}.${ext}`}
function setShareStatus(text){$('#shareStatus').textContent=text||''}
function setActionBusy(busy){document.querySelectorAll('.share-btn').forEach(b=>b.disabled=busy)}

async function uploadCard(blob, filename, format='png'){
  const key=format==='jpeg'?'jpg':'png';
  if(currentMedia[key]) return currentMedia[key];
  const form=new FormData(); form.append('file',blob,filename);
  const r=await fetch('/api/card-media',{method:'POST',headers:initDataHeaders(),body:form});
  let d=null; try{ d=await r.json(); }catch(_){ d=null; }
  if(!d) throw new Error(r.status>=500?'Server সাময়িকভাবে unavailable। একটু পরে আবার চেষ্টা করুন।':'Card image upload failed.');
  if(!r.ok||!d.ok) throw new Error(d.error||'Card image upload failed.');
  currentMedia[key]={url:d.url,downloadUrl:d.download_url,filename};
  return currentMedia[key];
}

async function downloadCard(){
  setActionBusy(true); setShareStatus('Preparing PNG…');
  try{
    const {blob}=await renderCardBlob('png');
    const filename=safeFilename('png');
    if(tg?.downloadFile && tg?.initData){
      const media=await uploadCard(blob,filename,'png');
      tg.downloadFile({url:media.downloadUrl||media.url,file_name:filename},ok=>setShareStatus(ok?'Download started.':'Download cancelled.'));
    }else{
      const url=URL.createObjectURL(blob); const a=document.createElement('a'); a.href=url; a.download=filename; document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(url),2000); setShareStatus('PNG download started.');
    }
  }catch(e){notify(e.message);setShareStatus('')}
  finally{setActionBusy(false)}
}

async function shareToStory(){
  if(!tg?.shareToStory){notify('আপনার Telegram version-এ Story sharing support নেই। Telegram update করে আবার চেষ্টা করুন।');return}
  setActionBusy(true); setShareStatus('Preparing Story…');
  try{
    const {blob}=await renderCardBlob('png'); const media=await uploadCard(blob,safeFilename('png'),'png');
    const isPremium=!!tg.initDataUnsafe?.user?.is_premium;
    const params={text:`${$('#cardName').textContent} • ODVUT INFO Activity`};
    // Story links (widget_link) are a Telegram Premium feature; omit it for non-Premium users
    // so the story itself still works instead of failing.
    if(isPremium) params.widget_link={url:window.location.origin,text:'ODVUT INFO'};
    try{ tg.shareToStory(media.url,params); }
    catch(err){ throw new Error('Story editor খোলা যায়নি। Telegram app update করে আবার চেষ্টা করুন।'); }
    setShareStatus(isPremium?'Story editor opened.':'Story editor opened. (Story-তে website link শুধু Telegram Premium-এ যোগ হয়)');
  }catch(e){notify(e.message);setShareStatus('')}
  finally{setActionBusy(false)}
}

async function shareToGroup(){
  if(!tg?.shareMessage){notify('আপনার Telegram version-এ media sharing support নেই। Telegram update করে আবার চেষ্টা করুন।');return}
  setActionBusy(true); setShareStatus('Preparing group share…');
  try{
    const {blob}=await renderCardBlob('jpeg'); const media=await uploadCard(blob,safeFilename('jpg'),'jpeg');
    const r=await fetch('/api/prepare-share',{method:'POST',headers:{...initDataHeaders(),'Content-Type':'application/json'},body:JSON.stringify({media_url:media.url,caption:`${$('#cardName').textContent} • ODVUT INFO Activity`})});
    let d=null; try{ d=await r.json(); }catch(_){ d=null; }
    if(!d) throw new Error(r.status>=500?'Server সাময়িকভাবে unavailable। একটু পরে আবার চেষ্টা করুন।':'Could not prepare group share.');
    if(r.status===410||r.status===413){ currentMedia.jpg=null; }
    if(!r.ok||!d.ok) throw new Error(d.error||'Could not prepare group share.');
    setShareStatus('Choose a chat in Telegram…');
    tg.shareMessage(d.id,ok=>setShareStatus(ok?'Activity Card shared successfully.':'Share cancelled.'));
  }catch(e){notify(e.message);setShareStatus('')}
  finally{setActionBusy(false)}
}
if(tg?.onEvent){
  try{
    tg.onEvent('shareMessageSent',()=>setShareStatus('Activity Card shared successfully.'));
    tg.onEvent('shareMessageFailed',(e)=>{ const err=String(e?.error||''); setShareStatus(err==='USER_DECLINED'?'Share cancelled.':'Share failed. আবার চেষ্টা করুন।'); });
  }catch(_){}
}

function close(id){
  const modal=$(id);
  modal.classList.add('hidden'); modal.setAttribute('aria-hidden','true');
  document.body.style.overflow='';
  currentMedia={};
  setShareStatus('');
  if(lastFocus && typeof lastFocus.focus==='function'){ try{ lastFocus.focus({preventScroll:true}); }catch(_){} }
  lastFocus=null;
}
document.addEventListener('keydown',e=>{ if(e.key==='Escape' && !$('#cardModal').classList.contains('hidden')) close('#cardModal'); });

// ---------------------------------------------------------------------------
// Theme: saved preference > Telegram colorScheme (inside Telegram only) > dark (existing default).
// ---------------------------------------------------------------------------
const THEME_BG={dark:'#080b14',light:'#f5f7fb'};
function applyTelegramColors(mode){
  if(!tg || !inTelegram) return;
  try{ tg.setHeaderColor?.(THEME_BG[mode]); }catch(_){}
  try{ tg.setBackgroundColor?.(THEME_BG[mode]); }catch(_){}
  try{ tg.setBottomBarColor?.(THEME_BG[mode]); }catch(_){}
}
function setTheme(mode, persist=true){
  mode = mode==='light'?'light':'dark';
  document.documentElement.dataset.theme=mode;
  if(persist) localStorage.setItem('odvut-theme',mode);
  $('#themeToggle').textContent=mode==='dark'?'☀️':'🌙';
  const meta=document.querySelector('meta[name="theme-color"]'); if(meta) meta.setAttribute('content',THEME_BG[mode]);
  applyTelegramColors(mode);
}
function initialTheme(){
  const saved=localStorage.getItem('odvut-theme');
  if(saved==='dark'||saved==='light') return saved;
  if(inTelegram && tg?.colorScheme) return tg.colorScheme==='light'?'light':'dark';
  return 'dark'; // existing default
}
setTheme(initialTheme(), false);
if(inTelegram && tg?.onEvent){ try{ tg.onEvent('themeChanged',()=>{ if(!localStorage.getItem('odvut-theme')) setTheme(tg.colorScheme==='light'?'light':'dark', false); }); }catch(_){} }
$('#themeToggle').addEventListener('click',()=>setTheme(document.documentElement.dataset.theme==='dark'?'light':'dark'));

$('#refresh').addEventListener('click',load); monthEl.addEventListener('change',load); $('#getMyCard').addEventListener('click',getMyActivityCard);
$('#closeCard').addEventListener('click',()=>close('#cardModal')); document.querySelectorAll('[data-close-card]').forEach(x=>x.addEventListener('click',()=>close('#cardModal')));
$('#downloadCard').addEventListener('click',downloadCard); $('#storyCard').addEventListener('click',shareToStory); $('#groupCard').addEventListener('click',shareToGroup);
load();
