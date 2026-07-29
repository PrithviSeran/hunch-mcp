document.getElementById('themeBtn').addEventListener('click',function(){
  var r=document.documentElement,d=r.getAttribute('data-theme')==='dark';
  if(d){r.removeAttribute('data-theme');}else{r.setAttribute('data-theme','dark');}
  try{localStorage.setItem('hunch-theme',d?'light':'dark');}catch(e){}
});
