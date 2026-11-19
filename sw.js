// Service worker minimo: serve per poter installare l'app.
// Solo le pagine (navigazione) passano dalla rete, con copia di riserva.
// I dati (tasselli, costa) NON vengono memorizzati: l'offline non è previsto.
var CACHE='isobate-guscio-v1';
self.addEventListener('install',function(){self.skipWaiting();});
self.addEventListener('activate',function(e){
 e.waitUntil(caches.keys().then(function(ks){return Promise.all(ks.filter(function(k){return k!==CACHE;}).map(function(k){return caches.delete(k);}));}).then(function(){return self.clients.claim();}));
});
self.addEventListener('fetch',function(e){
 if(e.request.mode!=='navigate')return;
 e.respondWith(fetch(e.request).then(function(r){
  var c=r.clone();caches.open(CACHE).then(function(ch){ch.put(e.request,c);});return r;
 }).catch(function(){return caches.match(e.request).then(function(m){return m||caches.match('./');});}));
});
