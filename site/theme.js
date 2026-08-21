document.getElementById('themeBtn').addEventListener('click',function(){
  var r=document.documentElement,d=r.getAttribute('data-theme')==='dark';
  if(d){r.removeAttribute('data-theme');}else{r.setAttribute('data-theme','dark');}
  try{localStorage.setItem('hunch-theme',d?'light':'dark');}catch(e){}
});

(function(){
  var image=document.querySelector('.hero-visual img');
  if(!image) return;
  var host=image.parentNode;
  var video=document.createElement('video');
  host.classList.add('hero-video-frame');
  host.setAttribute('aria-label','Hunch demo reel');
  host.innerHTML='';
  video.className='hero-video is-active';
  video.src='/assets/hero-demo-combined.mp4';
  video.muted=true;
  video.autoplay=true;
  video.loop=true;
  video.playsInline=true;
  video.preload='auto';
  video.setAttribute('aria-hidden','true');
  host.appendChild(video);
  video.load();
  var promise=video.play();
  if(promise && promise.catch) promise.catch(function(){});
})();
