import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
const developmentApi=process.env.SELINCO_DEV_API||'http://127.0.0.1:8881'
function localProxy(){return {target:developmentApi,changeOrigin:true,configure(proxy){
 proxy.on('proxyReq',(outgoing,incoming)=>{
  const origin=incoming.headers.origin
  // Only translate requests that were same-origin at the frontend boundary.
  // An external Origin is preserved so the backend still rejects it.
  if(origin){try{if(new URL(origin).host===incoming.headers.host)outgoing.setHeader('origin',new URL(developmentApi).origin)}catch{}}
 })
}}}
export default defineConfig({plugins:[vue()],server:{proxy:{'/api':localProxy(),'/spatial':localProxy()}},build:{emptyOutDir:false,chunkSizeWarningLimit:1200}})
