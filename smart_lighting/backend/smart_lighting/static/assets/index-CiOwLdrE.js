(function(){let e=document.createElement(`link`).relList;if(e&&e.supports&&e.supports(`modulepreload`))return;for(let e of document.querySelectorAll(`link[rel="modulepreload"]`))n(e);new MutationObserver(e=>{for(let t of e)if(t.type===`childList`)for(let e of t.addedNodes)e.tagName===`LINK`&&e.rel===`modulepreload`&&n(e)}).observe(document,{childList:!0,subtree:!0});function t(e){let t={};return e.integrity&&(t.integrity=e.integrity),e.referrerPolicy&&(t.referrerPolicy=e.referrerPolicy),t.credentials=e.crossOrigin===`use-credentials`?`include`:e.crossOrigin===`anonymous`?`omit`:`same-origin`,t}function n(e){if(e.ep)return;e.ep=!0;let n=t(e);fetch(e.href,n)}})();var e=`smart-lighting.theme`,t=`smart-lighting.advanced`;function n(e){try{return window.localStorage.getItem(e)}catch{return null}}function r(e,t){try{window.localStorage.setItem(e,t)}catch{}}var i=(()=>{let t=n(e);return t===`light`||t===`dark`?t:`auto`})(),a=n(t)===`1`;function o(){return i}function s(t){i=t,r(e,t),c()}function c(){let e=document.documentElement;i===`auto`?e.removeAttribute(`data-theme`):e.setAttribute(`data-theme`,i)}function l(){return a}function u(e){a=e,r(t,e?`1`:`0`)}function d(e){return new URL(e,document.baseURI).toString()}async function f(e){let t=await fetch(d(e),{cache:`no-store`,credentials:`same-origin`});if(!t.ok)throw Error(`HTTP ${t.status}`);return await t.json()}var p=new class{constructor(){this.overview=null,this.link=`connecting`,this.linkError=null,this.lastUpdate=null,this.liveStates=new Map,this.roomsVersion=0,this.registriesVersion=0,this.roomsRevision=null,this.listeners=new Set}subscribe(e){return this.listeners.add(e),()=>this.listeners.delete(e)}emit(){for(let e of this.listeners)e()}setLink(e,t=null){(e!==this.link||t!==this.linkError)&&(this.link=e,this.linkError=t,this.emit())}setOverview(e){this.overview=e,this.lastUpdate=new Date,this.emit()}apply(e){switch(e.type){case`overview`:this.setOverview(e.overview);return;case`status`:if(!this.overview)return;this.overview={...this.overview,app:e.app,checks:e.checks,home_assistant:{connection:e.connection,info:e.info}};break;case`states`:for(let t of e.changed)this.liveStates.set(t.entity_id,t);break;case`rooms`:this.roomsVersion+=1,this.roomsRevision=e.status?.revision??null;break;case`registries`:this.registriesVersion+=1;break;case`entities`:{if(!this.overview)return;let t=new Map(e.changed.map(e=>[e.entity_id,e]));this.overview={...this.overview,integration_entities:this.overview.integration_entities.map(e=>t.get(e.entity_id)??e)};break}default:return}this.lastUpdate=new Date,this.emit()}},ee=[1,2,5,10,30],te=1e4,m=new class{constructor(){this.ws=null,this.watched=[],this.failures=0}watch(e){this.watched=[...new Set(e)].slice(0,2e3),this.sendWatch()}sendWatch(){this.ws?.readyState===WebSocket.OPEN&&this.ws.send(JSON.stringify({type:`watch`,entities:this.watched}))}start(){this.refresh(),this.open()}async refresh(){try{p.setOverview(await f(`api/overview`)),p.link===`offline`&&p.setLink(this.ws?.readyState===WebSocket.OPEN?`live`:`polling`)}catch(e){p.link!==`live`&&p.setLink(`offline`,String(e))}}open(){let e=new URL(`api/live`,document.baseURI);e.protocol=e.protocol===`https:`?`wss:`:`ws:`;let t;try{t=new WebSocket(e)}catch{this.scheduleRetry();return}this.ws=t,t.onopen=()=>{this.failures=0,this.stopPolling(),p.setLink(`live`),this.watched.length&&this.sendWatch()},t.onmessage=e=>{try{p.apply(JSON.parse(e.data))}catch{}},t.onclose=()=>{this.ws===t&&(this.ws=null),this.scheduleRetry()}}scheduleRetry(){this.failures+=1,this.failures>=2?this.startPolling():p.setLink(`connecting`);let e=ee[Math.min(this.failures-1,ee.length-1)]*1e3;window.clearTimeout(this.retryTimer),this.retryTimer=window.setTimeout(()=>this.open(),e)}startPolling(){this.pollTimer===void 0&&(p.link!==`offline`&&p.setLink(`polling`),this.pollTimer=window.setInterval(()=>void this.refresh(),te),this.refresh())}stopPolling(){window.clearInterval(this.pollTimer),this.pollTimer=void 0}},h=globalThis,ne=h.ShadowRoot&&(h.ShadyCSS===void 0||h.ShadyCSS.nativeShadow)&&`adoptedStyleSheets`in Document.prototype&&`replace`in CSSStyleSheet.prototype,re=Symbol(),ie=new WeakMap,ae=class{constructor(e,t,n){if(this._$cssResult$=!0,n!==re)throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");this.cssText=e,this.t=t}get styleSheet(){let e=this.o,t=this.t;if(ne&&e===void 0){let n=t!==void 0&&t.length===1;n&&(e=ie.get(t)),e===void 0&&((this.o=e=new CSSStyleSheet).replaceSync(this.cssText),n&&ie.set(t,e))}return e}toString(){return this.cssText}},oe=e=>new ae(typeof e==`string`?e:e+``,void 0,re),se=(e,t)=>{if(ne)e.adoptedStyleSheets=t.map(e=>e instanceof CSSStyleSheet?e:e.styleSheet);else for(let n of t){let t=document.createElement(`style`),r=h.litNonce;r!==void 0&&t.setAttribute(`nonce`,r),t.textContent=n.cssText,e.appendChild(t)}},ce=ne?e=>e:e=>e instanceof CSSStyleSheet?(e=>{let t=``;for(let n of e.cssRules)t+=n.cssText;return oe(t)})(e):e,{is:le,defineProperty:ue,getOwnPropertyDescriptor:de,getOwnPropertyNames:fe,getOwnPropertySymbols:pe,getPrototypeOf:me}=Object,g=globalThis,he=g.trustedTypes,ge=he?he.emptyScript:``,_e=g.reactiveElementPolyfillSupport,_=(e,t)=>e,ve={toAttribute(e,t){switch(t){case Boolean:e=e?ge:null;break;case Object:case Array:e=e==null?e:JSON.stringify(e)}return e},fromAttribute(e,t){let n=e;switch(t){case Boolean:n=e!==null;break;case Number:n=e===null?null:Number(e);break;case Object:case Array:try{n=JSON.parse(e)}catch{n=null}}return n}},ye=(e,t)=>!le(e,t),be={attribute:!0,type:String,converter:ve,reflect:!1,useDefault:!1,hasChanged:ye};Symbol.metadata??=Symbol(`metadata`),g.litPropertyMetadata??=new WeakMap;var v=class extends HTMLElement{static addInitializer(e){this._$Ei(),(this.l??=[]).push(e)}static get observedAttributes(){return this.finalize(),this._$Eh&&[...this._$Eh.keys()]}static createProperty(e,t=be){if(t.state&&(t.attribute=!1),this._$Ei(),this.prototype.hasOwnProperty(e)&&((t=Object.create(t)).wrapped=!0),this.elementProperties.set(e,t),!t.noAccessor){let n=Symbol(),r=this.getPropertyDescriptor(e,n,t);r!==void 0&&ue(this.prototype,e,r)}}static getPropertyDescriptor(e,t,n){let{get:r,set:i}=de(this.prototype,e)??{get(){return this[t]},set(e){this[t]=e}};return{get:r,set(t){let a=r?.call(this);i?.call(this,t),this.requestUpdate(e,a,n)},configurable:!0,enumerable:!0}}static getPropertyOptions(e){return this.elementProperties.get(e)??be}static _$Ei(){if(this.hasOwnProperty(_(`elementProperties`)))return;let e=me(this);e.finalize(),e.l!==void 0&&(this.l=[...e.l]),this.elementProperties=new Map(e.elementProperties)}static finalize(){if(this.hasOwnProperty(_(`finalized`)))return;if(this.finalized=!0,this._$Ei(),this.hasOwnProperty(_(`properties`))){let e=this.properties,t=[...fe(e),...pe(e)];for(let n of t)this.createProperty(n,e[n])}let e=this[Symbol.metadata];if(e!==null){let t=litPropertyMetadata.get(e);if(t!==void 0)for(let[e,n]of t)this.elementProperties.set(e,n)}this._$Eh=new Map;for(let[e,t]of this.elementProperties){let n=this._$Eu(e,t);n!==void 0&&this._$Eh.set(n,e)}this.elementStyles=this.finalizeStyles(this.styles)}static finalizeStyles(e){let t=[];if(Array.isArray(e)){let n=new Set(e.flat(1/0).reverse());for(let e of n)t.unshift(ce(e))}else e!==void 0&&t.push(ce(e));return t}static _$Eu(e,t){let n=t.attribute;return!1===n?void 0:typeof n==`string`?n:typeof e==`string`?e.toLowerCase():void 0}constructor(){super(),this._$Ep=void 0,this.isUpdatePending=!1,this.hasUpdated=!1,this._$Em=null,this._$Ev()}_$Ev(){this._$ES=new Promise(e=>this.enableUpdating=e),this._$AL=new Map,this._$E_(),this.requestUpdate(),this.constructor.l?.forEach(e=>e(this))}addController(e){(this._$EO??=new Set).add(e),this.renderRoot!==void 0&&this.isConnected&&e.hostConnected?.()}removeController(e){this._$EO?.delete(e)}_$E_(){let e=new Map,t=this.constructor.elementProperties;for(let n of t.keys())this.hasOwnProperty(n)&&(e.set(n,this[n]),delete this[n]);e.size>0&&(this._$Ep=e)}createRenderRoot(){let e=this.shadowRoot??this.attachShadow(this.constructor.shadowRootOptions);return se(e,this.constructor.elementStyles),e}connectedCallback(){this.renderRoot??=this.createRenderRoot(),this.enableUpdating(!0),this._$EO?.forEach(e=>e.hostConnected?.())}enableUpdating(e){}disconnectedCallback(){this._$EO?.forEach(e=>e.hostDisconnected?.())}attributeChangedCallback(e,t,n){this._$AK(e,n)}_$ET(e,t){let n=this.constructor.elementProperties.get(e),r=this.constructor._$Eu(e,n);if(r!==void 0&&!0===n.reflect){let i=(n.converter?.toAttribute===void 0?ve:n.converter).toAttribute(t,n.type);this._$Em=e,i==null?this.removeAttribute(r):this.setAttribute(r,i),this._$Em=null}}_$AK(e,t){let n=this.constructor,r=n._$Eh.get(e);if(r!==void 0&&this._$Em!==r){let e=n.getPropertyOptions(r),i=typeof e.converter==`function`?{fromAttribute:e.converter}:e.converter?.fromAttribute===void 0?ve:e.converter;this._$Em=r;let a=i.fromAttribute(t,e.type);this[r]=a??this._$Ej?.get(r)??a,this._$Em=null}}requestUpdate(e,t,n,r=!1,i){if(e!==void 0){let a=this.constructor;if(!1===r&&(i=this[e]),n??=a.getPropertyOptions(e),!((n.hasChanged??ye)(i,t)||n.useDefault&&n.reflect&&i===this._$Ej?.get(e)&&!this.hasAttribute(a._$Eu(e,n))))return;this.C(e,t,n)}!1===this.isUpdatePending&&(this._$ES=this._$EP())}C(e,t,{useDefault:n,reflect:r,wrapped:i},a){n&&!(this._$Ej??=new Map).has(e)&&(this._$Ej.set(e,a??t??this[e]),!0!==i||a!==void 0)||(this._$AL.has(e)||(this.hasUpdated||n||(t=void 0),this._$AL.set(e,t)),!0===r&&this._$Em!==e&&(this._$Eq??=new Set).add(e))}async _$EP(){this.isUpdatePending=!0;try{await this._$ES}catch(e){Promise.reject(e)}let e=this.scheduleUpdate();return e!=null&&await e,!this.isUpdatePending}scheduleUpdate(){return this.performUpdate()}performUpdate(){if(!this.isUpdatePending)return;if(!this.hasUpdated){if(this.renderRoot??=this.createRenderRoot(),this._$Ep){for(let[e,t]of this._$Ep)this[e]=t;this._$Ep=void 0}let e=this.constructor.elementProperties;if(e.size>0)for(let[t,n]of e){let{wrapped:e}=n,r=this[t];!0!==e||this._$AL.has(t)||r===void 0||this.C(t,void 0,n,r)}}let e=!1,t=this._$AL;try{e=this.shouldUpdate(t),e?(this.willUpdate(t),this._$EO?.forEach(e=>e.hostUpdate?.()),this.update(t)):this._$EM()}catch(t){throw e=!1,this._$EM(),t}e&&this._$AE(t)}willUpdate(e){}_$AE(e){this._$EO?.forEach(e=>e.hostUpdated?.()),this.hasUpdated||(this.hasUpdated=!0,this.firstUpdated(e)),this.updated(e)}_$EM(){this._$AL=new Map,this.isUpdatePending=!1}get updateComplete(){return this.getUpdateComplete()}getUpdateComplete(){return this._$ES}shouldUpdate(e){return!0}update(e){this._$Eq&&=this._$Eq.forEach(e=>this._$ET(e,this[e])),this._$EM()}updated(e){}firstUpdated(e){}};v.elementStyles=[],v.shadowRootOptions={mode:`open`},v[_(`elementProperties`)]=new Map,v[_(`finalized`)]=new Map,_e?.({ReactiveElement:v}),(g.reactiveElementVersions??=[]).push(`2.1.2`);var xe=globalThis,Se=e=>e,y=xe.trustedTypes,Ce=y?y.createPolicy(`lit-html`,{createHTML:e=>e}):void 0,we=`$lit$`,b=`lit$${Math.random().toFixed(9).slice(2)}$`,Te=`?`+b,Ee=`<${Te}>`,x=document,S=()=>x.createComment(``),C=e=>e===null||typeof e!=`object`&&typeof e!=`function`,De=Array.isArray,Oe=e=>De(e)||typeof e?.[Symbol.iterator]==`function`,ke=`[ 	
\f\r]`,w=/<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g,Ae=/-->/g,je=/>/g,T=RegExp(`>|${ke}(?:([^\\s"'>=/]+)(${ke}*=${ke}*(?:[^ \t\n\f\r"'\`<>=]|("|')|))|$)`,`g`),Me=/'/g,Ne=/"/g,Pe=/^(?:script|style|textarea|title)$/i,Fe=e=>(t,...n)=>({_$litType$:e,strings:t,values:n}),E=Fe(1),Ie=Fe(2),D=Symbol.for(`lit-noChange`),O=Symbol.for(`lit-nothing`),Le=new WeakMap,k=x.createTreeWalker(x,129);function Re(e,t){if(!De(e)||!e.hasOwnProperty(`raw`))throw Error(`invalid template strings array`);return Ce===void 0?t:Ce.createHTML(t)}var ze=(e,t)=>{let n=e.length-1,r=[],i,a=t===2?`<svg>`:t===3?`<math>`:``,o=w;for(let t=0;t<n;t++){let n=e[t],s,c,l=-1,u=0;for(;u<n.length&&(o.lastIndex=u,c=o.exec(n),c!==null);)u=o.lastIndex,o===w?c[1]===`!--`?o=Ae:c[1]===void 0?c[2]===void 0?c[3]!==void 0&&(o=T):(Pe.test(c[2])&&(i=RegExp(`</`+c[2],`g`)),o=T):o=je:o===T?c[0]===`>`?(o=i??w,l=-1):c[1]===void 0?l=-2:(l=o.lastIndex-c[2].length,s=c[1],o=c[3]===void 0?T:c[3]===`"`?Ne:Me):o===Ne||o===Me?o=T:o===Ae||o===je?o=w:(o=T,i=void 0);let d=o===T&&e[t+1].startsWith(`/>`)?` `:``;a+=o===w?n+Ee:l>=0?(r.push(s),n.slice(0,l)+we+n.slice(l)+b+d):n+b+(l===-2?t:d)}return[Re(e,a+(e[n]||`<?>`)+(t===2?`</svg>`:t===3?`</math>`:``)),r]},Be=class e{constructor({strings:t,_$litType$:n},r){let i;this.parts=[];let a=0,o=0,s=t.length-1,c=this.parts,[l,u]=ze(t,n);if(this.el=e.createElement(l,r),k.currentNode=this.el.content,n===2||n===3){let e=this.el.content.firstChild;e.replaceWith(...e.childNodes)}for(;(i=k.nextNode())!==null&&c.length<s;){if(i.nodeType===1){if(i.hasAttributes())for(let e of i.getAttributeNames())if(e.endsWith(we)){let t=u[o++],n=i.getAttribute(e).split(b),r=/([.?@])?(.*)/.exec(t);c.push({type:1,index:a,name:r[2],strings:n,ctor:r[1]===`.`?Ue:r[1]===`?`?We:r[1]===`@`?Ge:j}),i.removeAttribute(e)}else e.startsWith(b)&&(c.push({type:6,index:a}),i.removeAttribute(e));if(Pe.test(i.tagName)){let e=i.textContent.split(b),t=e.length-1;if(t>0){i.textContent=y?y.emptyScript:``;for(let n=0;n<t;n++)i.append(e[n],S()),k.nextNode(),c.push({type:2,index:++a});i.append(e[t],S())}}}else if(i.nodeType===8){if(i.data===Te)c.push({type:2,index:a});else{let e=-1;for(;(e=i.data.indexOf(b,e+1))!==-1;)c.push({type:7,index:a}),e+=b.length-1}}a++}}static createElement(e,t){let n=x.createElement(`template`);return n.innerHTML=e,n}};function A(e,t,n=e,r){if(t===D)return t;let i=r===void 0?n._$Cl:n._$Co?.[r],a=C(t)?void 0:t._$litDirective$;return i?.constructor!==a&&(i?._$AO?.(!1),a===void 0?i=void 0:(i=new a(e),i._$AT(e,n,r)),r===void 0?n._$Cl=i:(n._$Co??=[])[r]=i),i!==void 0&&(t=A(e,i._$AS(e,t.values),i,r)),t}var Ve=class{constructor(e,t){this._$AV=[],this._$AN=void 0,this._$AD=e,this._$AM=t}get parentNode(){return this._$AM.parentNode}get _$AU(){return this._$AM._$AU}u(e){let{el:{content:t},parts:n}=this._$AD,r=(e?.creationScope??x).importNode(t,!0);k.currentNode=r;let i=k.nextNode(),a=0,o=0,s=n[0];for(;s!==void 0;){if(a===s.index){let t;s.type===2?t=new He(i,i.nextSibling,this,e):s.type===1?t=new s.ctor(i,s.name,s.strings,this,e):s.type===6&&(t=new Ke(i,this,e)),this._$AV.push(t),s=n[++o]}a!==s?.index&&(i=k.nextNode(),a++)}return k.currentNode=x,r}p(e){let t=0;for(let n of this._$AV)n!==void 0&&(n.strings===void 0?n._$AI(e[t]):(n._$AI(e,n,t),t+=n.strings.length-2)),t++}},He=class e{get _$AU(){return this._$AM?._$AU??this._$Cv}constructor(e,t,n,r){this.type=2,this._$AH=O,this._$AN=void 0,this._$AA=e,this._$AB=t,this._$AM=n,this.options=r,this._$Cv=r?.isConnected??!0}get parentNode(){let e=this._$AA.parentNode,t=this._$AM;return t!==void 0&&e?.nodeType===11&&(e=t.parentNode),e}get startNode(){return this._$AA}get endNode(){return this._$AB}_$AI(e,t=this){e=A(this,e,t),C(e)?e===O||e==null||e===``?(this._$AH!==O&&this._$AR(),this._$AH=O):e!==this._$AH&&e!==D&&this._(e):e._$litType$===void 0?e.nodeType===void 0?Oe(e)?this.k(e):this._(e):this.T(e):this.$(e)}O(e){return this._$AA.parentNode.insertBefore(e,this._$AB)}T(e){this._$AH!==e&&(this._$AR(),this._$AH=this.O(e))}_(e){this._$AH!==O&&C(this._$AH)?this._$AA.nextSibling.data=e:this.T(x.createTextNode(e)),this._$AH=e}$(e){let{values:t,_$litType$:n}=e,r=typeof n==`number`?this._$AC(e):(n.el===void 0&&(n.el=Be.createElement(Re(n.h,n.h[0]),this.options)),n);if(this._$AH?._$AD===r)this._$AH.p(t);else{let e=new Ve(r,this),n=e.u(this.options);e.p(t),this.T(n),this._$AH=e}}_$AC(e){let t=Le.get(e.strings);return t===void 0&&Le.set(e.strings,t=new Be(e)),t}k(t){De(this._$AH)||(this._$AH=[],this._$AR());let n=this._$AH,r,i=0;for(let a of t)i===n.length?n.push(r=new e(this.O(S()),this.O(S()),this,this.options)):r=n[i],r._$AI(a),i++;i<n.length&&(this._$AR(r&&r._$AB.nextSibling,i),n.length=i)}_$AR(e=this._$AA.nextSibling,t){for(this._$AP?.(!1,!0,t);e!==this._$AB;){let t=Se(e).nextSibling;Se(e).remove(),e=t}}setConnected(e){this._$AM===void 0&&(this._$Cv=e,this._$AP?.(e))}},j=class{get tagName(){return this.element.tagName}get _$AU(){return this._$AM._$AU}constructor(e,t,n,r,i){this.type=1,this._$AH=O,this._$AN=void 0,this.element=e,this.name=t,this._$AM=r,this.options=i,n.length>2||n[0]!==``||n[1]!==``?(this._$AH=Array(n.length-1).fill(new String),this.strings=n):this._$AH=O}_$AI(e,t=this,n,r){let i=this.strings,a=!1;if(i===void 0)e=A(this,e,t,0),a=!C(e)||e!==this._$AH&&e!==D,a&&(this._$AH=e);else{let r=e,o,s;for(e=i[0],o=0;o<i.length-1;o++)s=A(this,r[n+o],t,o),s===D&&(s=this._$AH[o]),a||=!C(s)||s!==this._$AH[o],s===O?e=O:e!==O&&(e+=(s??``)+i[o+1]),this._$AH[o]=s}a&&!r&&this.j(e)}j(e){e===O?this.element.removeAttribute(this.name):this.element.setAttribute(this.name,e??``)}},Ue=class extends j{constructor(){super(...arguments),this.type=3}j(e){this.element[this.name]=e===O?void 0:e}},We=class extends j{constructor(){super(...arguments),this.type=4}j(e){this.element.toggleAttribute(this.name,!!e&&e!==O)}},Ge=class extends j{constructor(e,t,n,r,i){super(e,t,n,r,i),this.type=5}_$AI(e,t=this){if((e=A(this,e,t,0)??O)===D)return;let n=this._$AH,r=e===O&&n!==O||e.capture!==n.capture||e.once!==n.once||e.passive!==n.passive,i=e!==O&&(n===O||r);r&&this.element.removeEventListener(this.name,this,n),i&&this.element.addEventListener(this.name,this,e),this._$AH=e}handleEvent(e){typeof this._$AH==`function`?this._$AH.call(this.options?.host??this.element,e):this._$AH.handleEvent(e)}},Ke=class{constructor(e,t,n){this.element=e,this.type=6,this._$AN=void 0,this._$AM=t,this.options=n}get _$AU(){return this._$AM._$AU}_$AI(e){A(this,e)}},qe=xe.litHtmlPolyfillSupport;qe?.(Be,He),(xe.litHtmlVersions??=[]).push(`3.3.3`);var Je=(e,t,n)=>{let r=n?.renderBefore??t,i=r._$litPart$;if(i===void 0){let e=n?.renderBefore??null;r._$litPart$=i=new He(t.insertBefore(S(),e),e,void 0,n??{})}return i._$AI(e),i},Ye=globalThis,M=class extends v{constructor(){super(...arguments),this.renderOptions={host:this},this._$Do=void 0}createRenderRoot(){let e=super.createRenderRoot();return this.renderOptions.renderBefore??=e.firstChild,e}update(e){let t=this.render();this.hasUpdated||(this.renderOptions.isConnected=this.isConnected),super.update(e),this._$Do=Je(t,this.renderRoot,this.renderOptions)}connectedCallback(){super.connectedCallback(),this._$Do?.setConnected(!0)}disconnectedCallback(){super.disconnectedCallback(),this._$Do?.setConnected(!1)}render(){return D}};M._$litElement$=!0,M.finalized=!0,Ye.litElementHydrateSupport?.({LitElement:M});var Xe=Ye.litElementPolyfillSupport;Xe?.({LitElement:M}),(Ye.litElementVersions??=[]).push(`4.2.2`);var Ze={"view-dashboard":`M13,3V9H21V3M13,21H21V11H13M3,21H11V15H3M3,13H11V3H3V13Z`,"floor-plan":`M10,5V10H9V5H5V13H9V12H10V17H9V14H5V19H12V17H13V19H19V17H21V21H3V3H21V15H19V10H13V15H12V9H19V5H10Z`,"motion-sensor":`M10,0.2C9,0.2 8.2,1 8.2,2C8.2,3 9,3.8 10,3.8C11,3.8 11.8,3 11.8,2C11.8,1 11,0.2 10,0.2M15.67,1A7.33,7.33 0 0,0 23,8.33V7A6,6 0 0,1 17,1H15.67M18.33,1C18.33,3.58 20.42,5.67 23,5.67V4.33C21.16,4.33 19.67,2.84 19.67,1H18.33M21,1A2,2 0 0,0 23,3V1H21M7.92,4.03C7.75,4.03 7.58,4.06 7.42,4.11L2,5.8V11H3.8V7.33L5.91,6.67L2,22H3.8L6.67,13.89L9,17V22H10.8V15.59L8.31,11.05L9.04,8.18L10.12,10H15V8.2H11.38L9.38,4.87C9.08,4.37 8.54,4.03 7.92,4.03Z`,"lightbulb-group":`M15 14V16A1 1 0 0 1 14 17H10A1 1 0 0 1 9 16V14A5 5 0 1 1 15 14M14 18H10V19A1 1 0 0 0 11 20H13A1 1 0 0 0 14 19M7 19V18H5V19A1 1 0 0 0 6 20H7.17A2.93 2.93 0 0 1 7 19M5 10A6.79 6.79 0 0 1 5.68 7A4 4 0 0 0 4 14.45V16A1 1 0 0 0 5 17H7V14.88A6.92 6.92 0 0 1 5 10M17 18V19A2.93 2.93 0 0 1 16.83 20H18A1 1 0 0 0 19 19V18M18.32 7A6.79 6.79 0 0 1 19 10A6.92 6.92 0 0 1 17 14.88V17H19A1 1 0 0 0 20 16V14.45A4 4 0 0 0 18.32 7Z`,palette:`M17.5,12A1.5,1.5 0 0,1 16,10.5A1.5,1.5 0 0,1 17.5,9A1.5,1.5 0 0,1 19,10.5A1.5,1.5 0 0,1 17.5,12M14.5,8A1.5,1.5 0 0,1 13,6.5A1.5,1.5 0 0,1 14.5,5A1.5,1.5 0 0,1 16,6.5A1.5,1.5 0 0,1 14.5,8M9.5,8A1.5,1.5 0 0,1 8,6.5A1.5,1.5 0 0,1 9.5,5A1.5,1.5 0 0,1 11,6.5A1.5,1.5 0 0,1 9.5,8M6.5,12A1.5,1.5 0 0,1 5,10.5A1.5,1.5 0 0,1 6.5,9A1.5,1.5 0 0,1 8,10.5A1.5,1.5 0 0,1 6.5,12M12,3A9,9 0 0,0 3,12A9,9 0 0,0 12,21A1.5,1.5 0 0,0 13.5,19.5C13.5,19.11 13.35,18.76 13.11,18.5C12.88,18.23 12.73,17.88 12.73,17.5A1.5,1.5 0 0,1 14.23,16H16A5,5 0 0,0 21,11C21,6.58 16.97,3 12,3Z`,"light-switch":`M8 6V18H16V6H8M14 10H10V8H14V10M19.4 1.6C19 1.2 18.5 1 18 1H6C5.5 1 5 1.2 4.6 1.6C4.2 2 4 2.5 4 3V21C4 21.5 4.2 22 4.6 22.4C5 22.8 5.5 23 6 23H18C18.5 23 19 22.8 19.4 22.4C19.8 22 20 21.5 20 21V3C20 2.5 19.8 2 19.4 1.6M18 21H6V3H18V21Z`,curtains:`M23 3H1V1H23V3M2 22H6C6 19 4 17 4 17C10 13 11 4 11 4H2V22M22 4H13C13 4 14 13 20 17C20 17 18 19 18 22H22V4Z`,"crystal-ball":`M9.38,8.38L11.5,9.34L13.62,8.38L12.66,10.5L13.62,12.62L11.5,11.66L9.38,12.62L10.34,10.5L9.38,8.38M16.5,2.5L17.59,5.41L20.5,6.5L17.59,7.59L16.5,10.5L15.41,7.59L12.5,6.5L15.41,5.41L16.5,2.5M6,19H7V18A1,1 0 0,1 8,17H8.26C6,15.7 4.5,13.28 4.5,10.5A7.5,7.5 0 0,1 12,3C13.05,3 14.05,3.22 14.96,3.61L14.59,4.59L13.17,5.12C12.79,5.04 12.4,5 12,5A5.5,5.5 0 0,0 6.5,10.5A5.5,5.5 0 0,0 12,16C14.91,16 17.3,13.73 17.5,10.87L18.41,8.41L19.12,8.14C19.37,8.88 19.5,9.68 19.5,10.5C19.5,13.28 18,15.7 15.74,17H16A1,1 0 0,1 17,18V19H18A2,2 0 0,1 20,21V22H4V21A2,2 0 0,1 6,19Z`,stethoscope:`M19,8C19.56,8 20,8.43 20,9A1,1 0 0,1 19,10C18.43,10 18,9.55 18,9C18,8.43 18.43,8 19,8M2,2V11C2,13.96 4.19,16.5 7.14,16.91C7.76,19.92 10.42,22 13.5,22A6.5,6.5 0 0,0 20,15.5V11.81C21.16,11.39 22,10.29 22,9A3,3 0 0,0 19,6A3,3 0 0,0 16,9C16,10.29 16.84,11.4 18,11.81V15.41C18,17.91 16,19.91 13.5,19.91C11.5,19.91 9.82,18.7 9.22,16.9C12,16.3 14,13.8 14,11V2H10V5H12V11A4,4 0 0,1 8,15A4,4 0 0,1 4,11V5H6V2H2Z`,cog:`M12,15.5A3.5,3.5 0 0,1 8.5,12A3.5,3.5 0 0,1 12,8.5A3.5,3.5 0 0,1 15.5,12A3.5,3.5 0 0,1 12,15.5M19.43,12.97C19.47,12.65 19.5,12.33 19.5,12C19.5,11.67 19.47,11.34 19.43,11L21.54,9.37C21.73,9.22 21.78,8.95 21.66,8.73L19.66,5.27C19.54,5.05 19.27,4.96 19.05,5.05L16.56,6.05C16.04,5.66 15.5,5.32 14.87,5.07L14.5,2.42C14.46,2.18 14.25,2 14,2H10C9.75,2 9.54,2.18 9.5,2.42L9.13,5.07C8.5,5.32 7.96,5.66 7.44,6.05L4.95,5.05C4.73,4.96 4.46,5.05 4.34,5.27L2.34,8.73C2.21,8.95 2.27,9.22 2.46,9.37L4.57,11C4.53,11.34 4.5,11.67 4.5,12C4.5,12.33 4.53,12.65 4.57,12.97L2.46,14.63C2.27,14.78 2.21,15.05 2.34,15.27L4.34,18.73C4.46,18.95 4.73,19.03 4.95,18.95L7.44,17.94C7.96,18.34 8.5,18.68 9.13,18.93L9.5,21.58C9.54,21.82 9.75,22 10,22H14C14.25,22 14.46,21.82 14.5,21.58L14.87,18.93C15.5,18.67 16.04,18.34 16.56,17.94L19.05,18.95C19.27,19.03 19.54,18.95 19.66,18.73L21.66,15.27C21.78,15.05 21.73,14.78 21.54,14.63L19.43,12.97Z`,"lightbulb-auto":`M9 2C5.13 2 2 5.13 2 9C2 11.38 3.19 13.47 5 14.74V17C5 17.55 5.45 18 6 18H12C12.55 18 13 17.55 13 17V14.74C14.81 13.47 16 11.38 16 9C16 5.13 12.87 2 9 2M6 21C6 21.55 6.45 22 7 22H11C11.55 22 12 21.55 12 21V20H6V21M19 13H17L13.8 22H15.7L16.4 20H19.6L20.3 22H22.2L19 13M16.85 18.65L18 15L19.15 18.65H16.85Z`,menu:`M3,6H21V8H3V6M3,11H21V13H3V11M3,16H21V18H3V16Z`,close:`M19,6.41L17.59,5L12,10.59L6.41,5L5,6.41L10.59,12L5,17.59L6.41,19L12,13.41L17.59,19L19,17.59L13.41,12L19,6.41Z`,ok:`M12 2C6.5 2 2 6.5 2 12S6.5 22 12 22 22 17.5 22 12 17.5 2 12 2M10 17L5 12L6.41 10.59L10 14.17L17.59 6.58L19 8L10 17Z`,warning:`M13 14H11V9H13M13 18H11V16H13M1 21H23L12 2L1 21Z`,error:`M13,13H11V7H13M13,17H11V15H13M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z`,info:`M13,9H11V7H13M13,17H11V11H13M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z`,"home-assistant":`M21.8,13H20V21H13V17.67L15.79,14.88L16.5,15C17.66,15 18.6,14.06 18.6,12.9C18.6,11.74 17.66,10.8 16.5,10.8A2.1,2.1 0 0,0 14.4,12.9L14.5,13.61L13,15.13V9.65C13.66,9.29 14.1,8.6 14.1,7.8A2.1,2.1 0 0,0 12,5.7A2.1,2.1 0 0,0 9.9,7.8C9.9,8.6 10.34,9.29 11,9.65V15.13L9.5,13.61L9.6,12.9A2.1,2.1 0 0,0 7.5,10.8A2.1,2.1 0 0,0 5.4,12.9A2.1,2.1 0 0,0 7.5,15L8.21,14.88L11,17.67V21H4V13H2.25C1.83,13 1.42,13 1.42,12.79C1.43,12.57 1.85,12.15 2.28,11.72L11,3C11.33,2.67 11.67,2.33 12,2.33C12.33,2.33 12.67,2.67 13,3L17,7V6H19V9L21.78,11.78C22.18,12.18 22.59,12.59 22.6,12.8C22.6,13 22.2,13 21.8,13M7.5,12A0.9,0.9 0 0,1 8.4,12.9A0.9,0.9 0 0,1 7.5,13.8A0.9,0.9 0 0,1 6.6,12.9A0.9,0.9 0 0,1 7.5,12M16.5,12C17,12 17.4,12.4 17.4,12.9C17.4,13.4 17,13.8 16.5,13.8A0.9,0.9 0 0,1 15.6,12.9A0.9,0.9 0 0,1 16.5,12M12,6.9C12.5,6.9 12.9,7.3 12.9,7.8C12.9,8.3 12.5,8.7 12,8.7C11.5,8.7 11.1,8.3 11.1,7.8C11.1,7.3 11.5,6.9 12,6.9Z`,puzzle:`M20.5,11H19V7C19,5.89 18.1,5 17,5H13V3.5A2.5,2.5 0 0,0 10.5,1A2.5,2.5 0 0,0 8,3.5V5H4A2,2 0 0,0 2,7V10.8H3.5C5,10.8 6.2,12 6.2,13.5C6.2,15 5,16.2 3.5,16.2H2V20A2,2 0 0,0 4,22H7.8V20.5C7.8,19 9,17.8 10.5,17.8C12,17.8 13.2,19 13.2,20.5V22H17A2,2 0 0,0 19,20V16H20.5A2.5,2.5 0 0,0 23,13.5A2.5,2.5 0 0,0 20.5,11Z`,download:`M5,20H19V18H5M19,9H15V3H9V9H5L12,16L19,9Z`,refresh:`M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z`,"chevron-right":`M8.59,16.58L13.17,12L8.59,7.41L10,6L16,12L10,18L8.59,16.58Z`,"shield-check":`M10,17L6,13L7.41,11.59L10,14.17L16.59,7.58L18,9M12,1L3,5V11C3,16.55 6.84,21.74 12,23C17.16,21.74 21,16.55 21,11V5L12,1Z`,pending:`M13,2.03V2.05L13,4.05C17.39,4.59 20.5,8.58 19.96,12.97C19.5,16.61 16.64,19.5 13,19.93V21.93C18.5,21.38 22.5,16.5 21.95,11C21.5,6.25 17.73,2.5 13,2.03M11,2.06C9.05,2.25 7.19,3 5.67,4.26L7.1,5.74C8.22,4.84 9.57,4.26 11,4.06V2.06M4.26,5.67C3,7.19 2.25,9.04 2.05,11H4.05C4.24,9.58 4.8,8.23 5.69,7.1L4.26,5.67M2.06,13C2.26,14.96 3.03,16.81 4.27,18.33L5.69,16.9C4.81,15.77 4.24,14.42 4.06,13H2.06M7.1,18.37L5.67,19.74C7.18,21 9.04,21.79 11,22V20C9.58,19.82 8.23,19.25 7.1,18.37M12.5,7V12.25L17,14.92L16.25,16.15L11,13V7H12.5Z`,search:`M9.5,3A6.5,6.5 0 0,1 16,9.5C16,11.11 15.41,12.59 14.44,13.73L14.71,14H15.5L20.5,19L19,20.5L14,15.5V14.71L13.73,14.44C12.59,15.41 11.11,16 9.5,16A6.5,6.5 0 0,1 3,9.5A6.5,6.5 0 0,1 9.5,3M9.5,5C7,5 5,7 5,9.5C5,12 7,14 9.5,14C12,14 14,12 14,9.5C14,7 12,5 9.5,5Z`,more:`M16,12A2,2 0 0,1 18,10A2,2 0 0,1 20,12A2,2 0 0,1 18,14A2,2 0 0,1 16,12M10,12A2,2 0 0,1 12,10A2,2 0 0,1 14,12A2,2 0 0,1 12,14A2,2 0 0,1 10,12M4,12A2,2 0 0,1 6,10A2,2 0 0,1 8,12A2,2 0 0,1 6,14A2,2 0 0,1 4,12Z`,copy:`M19,21H8V7H19M19,5H8A2,2 0 0,0 6,7V21A2,2 0 0,0 8,23H19A2,2 0 0,0 21,21V7A2,2 0 0,0 19,5M16,1H4A2,2 0 0,0 2,3V17H4V3H16V1Z`,json:`M5,3H7V5H5V10A2,2 0 0,1 3,12A2,2 0 0,1 5,14V19H7V21H5C3.93,20.73 3,20.1 3,19V15A2,2 0 0,0 1,13H0V11H1A2,2 0 0,0 3,9V5A2,2 0 0,1 5,3M19,3A2,2 0 0,1 21,5V9A2,2 0 0,0 23,11H24V13H23A2,2 0 0,0 21,15V19A2,2 0 0,1 19,21H17V19H19V14A2,2 0 0,1 21,12A2,2 0 0,1 19,10V5H17V3H19M12,15A1,1 0 0,1 13,16A1,1 0 0,1 12,17A1,1 0 0,1 11,16A1,1 0 0,1 12,15M8,15A1,1 0 0,1 9,16A1,1 0 0,1 8,17A1,1 0 0,1 7,16A1,1 0 0,1 8,15M16,15A1,1 0 0,1 17,16A1,1 0 0,1 16,17A1,1 0 0,1 15,16A1,1 0 0,1 16,15Z`,"menu-open":`M21,15.61L19.59,17L14.58,12L19.59,7L21,8.39L17.44,12L21,15.61M3,6H16V8H3V6M3,13V11H13V13H3M3,18V16H16V18H3Z`,radar:`M19.07,4.93L17.66,6.34C19.1,7.79 20,9.79 20,12A8,8 0 0,1 12,20A8,8 0 0,1 4,12C4,7.92 7.05,4.56 11,4.07V6.09C8.16,6.57 6,9.03 6,12A6,6 0 0,0 12,18A6,6 0 0,0 18,12C18,10.34 17.33,8.84 16.24,7.76L14.83,9.17C15.55,9.9 16,10.9 16,12A4,4 0 0,1 12,16A4,4 0 0,1 8,12C8,10.14 9.28,8.59 11,8.14V10.28C10.4,10.63 10,11.26 10,12A2,2 0 0,0 12,14A2,2 0 0,0 14,12C14,11.26 13.6,10.62 13,10.28V2H12A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12C22,9.24 20.88,6.74 19.07,4.93Z`,door:`M8,3C6.89,3 6,3.89 6,5V21H18V5C18,3.89 17.11,3 16,3H8M8,5H16V19H8V5M13,11V13H15V11H13Z`,window:`M21 20V2H3V20H1V23H23V20M19 4V11H13V4M5 4H11V11H5M5 20V13H11V20M13 20V13H19V20Z`,monitor:`M21,16H3V4H21M21,2H3C1.89,2 1,2.89 1,4V16A2,2 0 0,0 3,18H10V20H8V22H16V20H14V18H21A2,2 0 0,0 23,16V4C23,2.89 22.1,2 21,2Z`,bed:`M19,7H11V14H3V5H1V20H3V17H21V20H23V11A4,4 0 0,0 19,7M7,13A3,3 0 0,0 10,10A3,3 0 0,0 7,7A3,3 0 0,0 4,10A3,3 0 0,0 7,13Z`,account:`M12,4A4,4 0 0,1 16,8A4,4 0 0,1 12,12A4,4 0 0,1 8,8A4,4 0 0,1 12,4M12,14C16.42,14 20,15.79 20,18V20H4V18C4,15.79 7.58,14 12,14Z`,thermometer:`M15 13V5A3 3 0 0 0 9 5V13A5 5 0 1 0 15 13M12 4A1 1 0 0 1 13 5V8H11V5A1 1 0 0 1 12 4Z`,brightness:`M12,18V6A6,6 0 0,1 18,12A6,6 0 0,1 12,18M20,15.31L23.31,12L20,8.69V4H15.31L12,0.69L8.69,4H4V8.69L0.69,12L4,15.31V20H8.69L12,23.31L15.31,20H20V15.31Z`,lightbulb:`M12,2A7,7 0 0,0 5,9C5,11.38 6.19,13.47 8,14.74V17A1,1 0 0,0 9,18H15A1,1 0 0,0 16,17V14.74C17.81,13.47 19,11.38 19,9A7,7 0 0,0 12,2M9,21A1,1 0 0,0 10,22H14A1,1 0 0,0 15,21V20H9V21Z`,"electric-switch":`M1,11H3.17C3.58,9.83 4.69,9 6,9C6.65,9 7.25,9.21 7.74,9.56L14.44,4.87L15.58,6.5L8.89,11.2C8.96,11.45 9,11.72 9,12A3,3 0 0,1 6,15C4.69,15 3.58,14.17 3.17,13H1V11M23,11V13H20.83C20.42,14.17 19.31,15 18,15A3,3 0 0,1 15,12A3,3 0 0,1 18,9C19.31,9 20.42,9.83 20.83,11H23M6,11A1,1 0 0,0 5,12A1,1 0 0,0 6,13A1,1 0 0,0 7,12A1,1 0 0,0 6,11M18,11A1,1 0 0,0 17,12A1,1 0 0,0 18,13A1,1 0 0,0 19,12A1,1 0 0,0 18,11Z`,plus:`M19,13H13V19H11V13H5V11H11V5H13V11H19V13Z`,minus:`M19,13H5V11H19V13Z`,delete:`M19,4H15.5L14.5,3H9.5L8.5,4H5V6H19M6,19A2,2 0 0,0 8,21H16A2,2 0 0,0 18,19V7H6V19Z`,"arrow-left":`M20,11V13H8L13.5,18.5L12.08,19.92L4.16,12L12.08,4.08L13.5,5.5L8,11H20Z`,pencil:`M20.71,7.04C21.1,6.65 21.1,6 20.71,5.63L18.37,3.29C18,2.9 17.35,2.9 16.96,3.29L15.12,5.12L18.87,8.87M3,17.25V21H6.75L17.81,9.93L14.06,6.18L3,17.25Z`,check:`M21,7L9,19L3.5,13.5L4.91,12.09L9,16.17L19.59,5.59L21,7Z`,undo:`M12.5,8C9.85,8 7.45,9 5.6,10.6L2,7V16H11L7.38,12.38C8.77,11.22 10.54,10.5 12.5,10.5C16.04,10.5 19.05,12.81 20.1,16L22.47,15.22C21.08,11.03 17.15,8 12.5,8Z`,eye:`M12,9A3,3 0 0,0 9,12A3,3 0 0,0 12,15A3,3 0 0,0 15,12A3,3 0 0,0 12,9M12,17A5,5 0 0,1 7,12A5,5 0 0,1 12,7A5,5 0 0,1 17,12A5,5 0 0,1 12,17M12,4.5C7,4.5 2.73,7.61 1,12C2.73,16.39 7,19.5 12,19.5C17,19.5 21.27,16.39 23,12C21.27,7.61 17,4.5 12,4.5Z`,upload:`M9,16V10H5L12,3L19,10H15V16H9M5,20V18H19V20H5Z`,history:`M13.5,8H12V13L16.28,15.54L17,14.33L13.5,12.25V8M13,3A9,9 0 0,0 4,12H1L4.96,16.03L9,12H6A7,7 0 0,1 13,5A7,7 0 0,1 20,12A7,7 0 0,1 13,19C11.07,19 9.32,18.21 8.06,16.94L6.64,18.36C8.27,20 10.5,21 13,21A9,9 0 0,0 22,12A9,9 0 0,0 13,3`,filter:`M6,13H18V11H6M3,6V8H21V6M10,18H14V16H10V18Z`,sort:`M18 21L14 17H17V7H14L18 3L22 7H19V17H22M2 19V17H12V19M2 13V11H9V13M2 7V5H6V7H2Z`,"chevron-left":`M15.41,16.58L10.83,12L15.41,7.41L14,6L8,12L14,18L15.41,16.58Z`,"chevron-down":`M7.41,8.58L12,13.17L16.59,8.58L18,10L12,16L6,10L7.41,8.58Z`,link:`M10.59,13.41C11,13.8 11,14.44 10.59,14.83C10.2,15.22 9.56,15.22 9.17,14.83C7.22,12.88 7.22,9.71 9.17,7.76V7.76L12.71,4.22C14.66,2.27 17.83,2.27 19.78,4.22C21.73,6.17 21.73,9.34 19.78,11.29L18.29,12.78C18.3,11.96 18.17,11.14 17.89,10.36L18.36,9.88C19.54,8.71 19.54,6.81 18.36,5.64C17.19,4.46 15.29,4.46 14.12,5.64L10.59,9.17C9.41,10.34 9.41,12.24 10.59,13.41M13.41,9.17C13.8,8.78 14.44,8.78 14.83,9.17C16.78,11.12 16.78,14.29 14.83,16.24V16.24L11.29,19.78C9.34,21.73 6.17,21.73 4.22,19.78C2.27,17.83 2.27,14.66 4.22,12.71L5.71,11.22C5.7,12.04 5.83,12.86 6.11,13.65L5.64,14.12C4.46,15.29 4.46,17.19 5.64,18.36C6.81,19.54 8.71,19.54 9.88,18.36L13.41,14.83C14.59,13.66 14.59,11.76 13.41,10.59C13,10.2 13,9.56 13.41,9.17Z`,power:`M16.56,5.44L15.11,6.89C16.84,7.94 18,9.83 18,12A6,6 0 0,1 12,18A6,6 0 0,1 6,12C6,9.83 7.16,7.94 8.88,6.88L7.44,5.44C5.36,6.88 4,9.28 4,12A8,8 0 0,0 12,20A8,8 0 0,0 20,12C20,9.28 18.64,6.88 16.56,5.44M13,3H11V13H13`,sync:`M12,18A6,6 0 0,1 6,12C6,11 6.25,10.03 6.7,9.2L5.24,7.74C4.46,8.97 4,10.43 4,12A8,8 0 0,0 12,20V23L16,19L12,15M12,4V1L8,5L12,9V6A6,6 0 0,1 18,12C18,13 17.75,13.97 17.3,14.8L18.76,16.26C19.54,15.03 20,13.57 20,12A8,8 0 0,0 12,4Z`,help:`M11,18H13V16H11V18M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2M12,20C7.59,20 4,16.41 4,12C4,7.59 7.59,4 12,4C16.41,4 20,7.59 20,12C20,16.41 16.41,20 12,20M12,6A4,4 0 0,0 8,10H10A2,2 0 0,1 12,8A2,2 0 0,1 14,10C14,12 11,11.75 11,15H13C13,12.75 16,12.5 16,10A4,4 0 0,0 12,6Z`,"dock-left":`M20 4H4A2 2 0 0 0 2 6V18A2 2 0 0 0 4 20H20A2 2 0 0 0 22 18V6A2 2 0 0 0 20 4M20 18H9V6H20Z`,"dock-right":`M20 4H4A2 2 0 0 0 2 6V18A2 2 0 0 0 4 20H20A2 2 0 0 0 22 18V6A2 2 0 0 0 20 4M15 18H4V6H15Z`,offline:`M4,1C2.89,1 2,1.89 2,3V7C2,8.11 2.89,9 4,9H1V11H13V9H10C11.11,9 12,8.11 12,7V3C12,1.89 11.11,1 10,1H4M4,3H10V7H4V3M14,13C12.89,13 12,13.89 12,15V19C12,20.11 12.89,21 14,21H11V23H23V21H20C21.11,21 22,20.11 22,19V15C22,13.89 21.11,13 20,13H14M3.88,13.46L2.46,14.88L4.59,17L2.46,19.12L3.88,20.54L6,18.41L8.12,20.54L9.54,19.12L7.41,17L9.54,14.88L8.12,13.46L6,15.59L3.88,13.46M14,15H20V19H14V15Z`,sofa:`M12.5 7C12.5 5.89 13.39 5 14.5 5H18C19.1 5 20 5.9 20 7V9.16C18.84 9.57 18 10.67 18 11.97V14H12.5V7M6 11.96V14H11.5V7C11.5 5.89 10.61 5 9.5 5H6C4.9 5 4 5.9 4 7V9.15C5.16 9.56 6 10.67 6 11.96M20.66 10.03C19.68 10.19 19 11.12 19 12.12V15H5V12C5 10.9 4.11 10 3 10S1 10.9 1 12V17C1 18.1 1.9 19 3 19V21H5V19H19V21H21V19C22.1 19 23 18.1 23 17V12C23 10.79 21.91 9.82 20.66 10.03Z`,robot:`M12,2A2,2 0 0,1 14,4C14,4.74 13.6,5.39 13,5.73V7H14A7,7 0 0,1 21,14H22A1,1 0 0,1 23,15V18A1,1 0 0,1 22,19H21V20A2,2 0 0,1 19,22H5A2,2 0 0,1 3,20V19H2A1,1 0 0,1 1,18V15A1,1 0 0,1 2,14H3A7,7 0 0,1 10,7H11V5.73C10.4,5.39 10,4.74 10,4A2,2 0 0,1 12,2M7.5,13A2.5,2.5 0 0,0 5,15.5A2.5,2.5 0 0,0 7.5,18A2.5,2.5 0 0,0 10,15.5A2.5,2.5 0 0,0 7.5,13M16.5,13A2.5,2.5 0 0,0 14,15.5A2.5,2.5 0 0,0 16.5,18A2.5,2.5 0 0,0 19,15.5A2.5,2.5 0 0,0 16.5,13Z`,observe:`M12,9A3,3 0 0,1 15,12A3,3 0 0,1 12,15A3,3 0 0,1 9,12A3,3 0 0,1 12,9M12,4.5C17,4.5 21.27,7.61 23,12C21.27,16.39 17,19.5 12,19.5C7,19.5 2.73,16.39 1,12C2.73,7.61 7,4.5 12,4.5M3.18,12C4.83,15.36 8.24,17.5 12,17.5C15.76,17.5 19.17,15.36 20.82,12C19.17,8.64 15.76,6.5 12,6.5C8.24,6.5 4.83,8.64 3.18,12Z`,save:`M15,9H5V5H15M12,19A3,3 0 0,1 9,16A3,3 0 0,1 12,13A3,3 0 0,1 15,16A3,3 0 0,1 12,19M17,3H5C3.89,3 3,3.9 3,5V19A2,2 0 0,0 5,21H19A2,2 0 0,0 21,19V7L17,3Z`,restore:`M13,3A9,9 0 0,0 4,12H1L4.89,15.89L4.96,16.03L9,12H6A7,7 0 0,1 13,5A7,7 0 0,1 20,12A7,7 0 0,1 13,19C11.07,19 9.32,18.21 8.06,16.94L6.64,18.36C8.27,20 10.5,21 13,21A9,9 0 0,0 22,12A9,9 0 0,0 13,3Z`,"file-upload":`M14,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2M13.5,16V19H10.5V16H8L12,12L16,16H13.5M13,9V3.5L18.5,9H13Z`,"close-circle":`M12,2C17.53,2 22,6.47 22,12C22,17.53 17.53,22 12,22C6.47,22 2,17.53 2,12C2,6.47 6.47,2 12,2M15.59,7L12,10.59L8.41,7L7,8.41L10.59,12L7,15.59L8.41,17L12,13.41L15.59,17L17,15.59L13.41,12L17,8.41L15.59,7Z`,"alert-octagon":`M13 13H11V7H13M11 15H13V17H11M15.73 3H8.27L3 8.27V15.73L8.27 21H15.73L21 15.73V8.27L15.73 3Z`,swap:`M21,9L17,5V8H10V10H17V13M7,11L3,15L7,19V16H14V14H7V11Z`,"arrow-right":`M4,11V13H16L10.5,18.5L11.92,19.92L19.84,12L11.92,4.08L10.5,5.5L16,11H4Z`,tune:`M8 13C6.14 13 4.59 14.28 4.14 16H2V18H4.14C4.59 19.72 6.14 21 8 21S11.41 19.72 11.86 18H22V16H11.86C11.41 14.28 9.86 13 8 13M8 19C6.9 19 6 18.1 6 17C6 15.9 6.9 15 8 15S10 15.9 10 17C10 18.1 9.1 19 8 19M19.86 6C19.41 4.28 17.86 3 16 3S12.59 4.28 12.14 6H2V8H12.14C12.59 9.72 14.14 11 16 11S19.41 9.72 19.86 8H22V6H19.86M16 9C14.9 9 14 8.1 14 7C14 5.9 14.9 5 16 5S18 5.9 18 7C18 8.1 17.1 9 16 9Z`,grid:`M3,11H11V3H3M3,21H11V13H3M13,21H21V13H13M13,3V11H21V3`,list:`M7,5H21V7H7V5M7,13V11H21V13H7M4,4.5A1.5,1.5 0 0,1 5.5,6A1.5,1.5 0 0,1 4,7.5A1.5,1.5 0 0,1 2.5,6A1.5,1.5 0 0,1 4,4.5M4,10.5A1.5,1.5 0 0,1 5.5,12A1.5,1.5 0 0,1 4,13.5A1.5,1.5 0 0,1 2.5,12A1.5,1.5 0 0,1 4,10.5M7,19V17H21V19H7M4,16.5A1.5,1.5 0 0,1 5.5,18A1.5,1.5 0 0,1 4,19.5A1.5,1.5 0 0,1 2.5,18A1.5,1.5 0 0,1 4,16.5Z`,home:`M10,20V14H14V20H19V12H22L12,3L2,12H5V20H10Z`,remote:`M12,0C8.96,0 6.21,1.23 4.22,3.22L5.63,4.63C7.26,3 9.5,2 12,2C14.5,2 16.74,3 18.36,4.64L19.77,3.23C17.79,1.23 15.04,0 12,0M7.05,6.05L8.46,7.46C9.37,6.56 10.62,6 12,6C13.38,6 14.63,6.56 15.54,7.46L16.95,6.05C15.68,4.78 13.93,4 12,4C10.07,4 8.32,4.78 7.05,6.05M12,15A2,2 0 0,1 10,13A2,2 0 0,1 12,11A2,2 0 0,1 14,13A2,2 0 0,1 12,15M15,9H9A1,1 0 0,0 8,10V22A1,1 0 0,0 9,23H15A1,1 0 0,0 16,22V10A1,1 0 0,0 15,9Z`,blinds:`M20 19V3H4V19H2V21H22V19H20M16 9H18V11H16V9M14 11H6V9H14V11M18 7H16V5H18V7M14 5V7H6V5H14M6 19V13H14V14.82C13.55 15.14 13.25 15.66 13.25 16.25C13.25 17.22 14.03 18 15 18S16.75 17.22 16.75 16.25C16.75 15.66 16.45 15.13 16 14.82V13H18V19H6Z`,"shield-alert":`M12,1L3,5V11C3,16.55 6.84,21.74 12,23C17.16,21.74 21,16.55 21,11V5M11,7H13V13H11M11,15H13V17H11`,export:`M12,3C7.58,3 4,4.79 4,7C4,9.21 7.58,11 12,11C12.5,11 13,10.97 13.5,10.92V9.5H16.39L15.39,8.5L18.9,5C17.5,3.8 14.94,3 12,3M18.92,7.08L17.5,8.5L20,11H15V13H20L17.5,15.5L18.92,16.92L23.84,12M4,9V12C4,14.21 7.58,16 12,16C13.17,16 14.26,15.85 15.25,15.63L16.38,14.5H13.5V12.92C13,12.97 12.5,13 12,13C7.58,13 4,11.21 4,9M4,14V17C4,19.21 7.58,21 12,21C14.94,21 17.5,20.2 18.9,19L17,17.1C15.61,17.66 13.9,18 12,18C7.58,18 4,16.21 4,14Z`};function N(e,t=24){return Ie`<svg class="icon" width=${t} height=${t} viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d=${Ze[e]??`M20.5,11H19V7C19,5.89 18.1,5 17,5H13V3.5A2.5,2.5 0 0,0 10.5,1A2.5,2.5 0 0,0 8,3.5V5H4A2,2 0 0,0 2,7V10.8H3.5C5,10.8 6.2,12 6.2,13.5C6.2,15 5,16.2 3.5,16.2H2V20A2,2 0 0,0 4,22H7.8V20.5C7.8,19 9,17.8 10.5,17.8C12,17.8 13.2,19 13.2,20.5V22H17A2,2 0 0,0 19,20V16H20.5A2.5,2.5 0 0,0 23,13.5A2.5,2.5 0 0,0 20.5,11Z`}></path></svg>`}var P=[{key:`dashboard`,title:`Dashboard`,icon:`view-dashboard`},{key:`rooms`,title:`Rooms`,icon:`floor-plan`},{key:`presence`,title:`Presence`,icon:`motion-sensor`},{key:`lighting`,title:`Lighting`,icon:`lightbulb-group`},{key:`moods`,title:`Moods`,icon:`palette`},{key:`switches`,title:`Switches`,icon:`light-switch`},{key:`curtains`,title:`Curtains`,icon:`curtains`},{key:`predictions`,title:`Predictions`,icon:`crystal-ball`},{key:`diagnostics`,title:`Diagnostics`,icon:`stethoscope`},{key:`settings`,title:`Settings`,icon:`cog`}],Qe=new Set([`dashboard`,`diagnostics`,`settings`]),$e=[`dashboard`,`rooms`,`lighting`,`moods`];function et(){let e=window.location.hash.replace(/^#\/?/,``).split(/[/?]/)[0];return P.some(t=>t.key===e)?e:`dashboard`}function tt(){return window.location.hash.replace(/^#\/?/,``).split(`?`)[0].split(`/`).slice(1).filter(Boolean).map(e=>decodeURIComponent(e))}function nt(e){window.location.hash!==`#/${e}`&&(window.location.hash=`/${e}`)}function rt(...e){window.location.hash=`/`+e.map(e=>encodeURIComponent(e)).join(`/`)}var it=null;function at(e){it=e}function ot(){return it?it():null}var F=class extends M{constructor(...e){super(...e),this.unsubscribe=null}createRenderRoot(){return this}connectedCallback(){super.connectedCallback(),this.unsubscribe=p.subscribe(()=>this.requestUpdate())}disconnectedCallback(){super.disconnectedCallback(),this.unsubscribe?.(),this.unsubscribe=null}};function I(e,t){return E`<span class="chip chip-${e}"><span class="dot" aria-hidden="true"></span><span class="chip-label">${t}</span></span>`}function st(e){return E`<span class="status-icon tone-${e}" role="img" aria-label=${e}>${N(e,20)}</span>`}function L(e){return E`<dl class="kv">
    ${e.map(e=>e?E`<dt>${e[0]}</dt>
            <dd>${e[1]===null||e[1]===void 0||e[1]===``?`—`:e[1]}</dd>`:O)}
  </dl>`}function R(e,t,n=O){return E`<div class="page-header">
    <div>
      <h2 tabindex="-1">${e}</h2>
      <p class="muted">${t}</p>
    </div>
    ${n}
  </div>`}function z(e){switch(e){case`live`:return{tone:`ok`,label:`Live`};case`polling`:return{tone:`warning`,label:`Updating every 10 s`};case`offline`:return{tone:`error`,label:`App unreachable`};default:return{tone:`neutral`,label:`Connecting…`}}}function B(e){switch(e?.state){case`connected`:return{tone:`ok`,label:`Connected`};case`connecting`:return{tone:`warning`,label:`Connecting…`};case`disconnected`:return{tone:`warning`,label:`Reconnecting…`};case`auth_failed`:return{tone:`error`,label:`Access refused`};case`not_configured`:return{tone:`error`,label:`No credentials`};default:return{tone:`neutral`,label:`Unknown`}}}function ct(e){switch(e?.status){case`loaded`:return{tone:`ok`,label:`Detected · running`};case`installed`:return{tone:`info`,label:`Installed · not set up`};case`not_running`:return{tone:`warning`,label:`Set up · not running`};case`not_installed`:return{tone:`neutral`,label:`Not installed`};default:return{tone:`neutral`,label:`Not checked yet`}}}function lt(){return E`<div class="card loading" role="status">
    <span class="spinner" aria-hidden="true"></span>
    ${p.link===`offline`?E`<span>The App backend cannot be reached. Retrying…</span>`:E`<span>Loading…</span>`}
  </div>`}var ut=new Intl.DateTimeFormat(void 0,{hour:`2-digit`,minute:`2-digit`,hourCycle:`h23`}),dt=new Intl.DateTimeFormat(void 0,{day:`numeric`,month:`short`,hour:`2-digit`,minute:`2-digit`,hourCycle:`h23`});function V(e){if(!e)return`—`;let t=new Date(e);if(Number.isNaN(t.getTime()))return`—`;let n=new Date;return t.toDateString()===n.toDateString()?ut.format(t):dt.format(t)}function ft(e){if(e==null||e<0)return`—`;if(e<60)return`${Math.floor(e)} s`;let t=Math.floor(e/60);if(t<60)return`${t} min`;let n=Math.floor(t/60);return n<24?`${n} h ${t%60} min`:`${Math.floor(n/24)} d ${n%24} h`}function pt(e,t){return e===null?`Not available`:e===`unavailable`?`Unavailable`:e===`unknown`?`Unknown`:t?`${e} ${t}`:e}function H(e,t,n){return`${e} ${e===1?t:n}`}var mt={ok:{tone:`ok`,label:`Healthy`},warning:{tone:`warning`,label:`Degraded`},error:{tone:`error`,label:`Problem`}},ht=class extends F{renderConnection(e){let t=e.home_assistant.connection,n=e.home_assistant.info,r=B(t),i=z(p.link);return E`<section class="card" data-testid="card-connection">
      <header class="card-head">
        ${N(`home-assistant`)}
        <h3>Home Assistant</h3>
        ${I(r.tone,r.label)}
      </header>
      ${t.state!==`connected`&&t.last_error?E`<p class="notice notice-${r.tone===`error`?`error`:`warning`}">${t.last_error}</p>`:O}
      ${L([[`Home`,n?.location_name],[`Version`,n?.version??t.ha_version],[`Connected since`,V(t.connected_since)],[`Response time`,t.latency_ms==null?null:`${t.latency_ms} ms`],[`Entities / areas`,n?`${n.entity_count??`—`} / ${n.area_count??`—`}`:null],[`State changes`,t.connected?`${t.events_per_minute??0} in the last minute`:null],[`Live updates`,i.label]])}
    </section>`}renderApp(e){let t=mt[e.app.health]??mt.warning,n=e.checks.filter(e=>e.status===`error`||e.status===`warning`);return E`<section class="card" data-testid="card-app">
      <header class="card-head">
        ${N(`lightbulb-auto`)}
        <h3>App</h3>
        ${I(t.tone,t.label)}
      </header>
      ${L([[`Version`,e.app.version],[`Phase`,`${e.app.phase} · Rooms configuration`],[`Running for`,ft(e.app.uptime_s)],[`Health checks`,n.length?H(n.length,`needs attention`,`need attention`):`All passed`]])}
      ${n.length?E`<ul class="plain-list">
            ${n.map(e=>E`<li>${I(e.status,e.title)} <span class="muted">${e.detail}</span></li>`)}
          </ul>`:O}
      <a class="text-link" href="#/diagnostics" @click=${e=>(e.preventDefault(),nt(`diagnostics`))}
        >Open Diagnostics ${N(`chevron-right`,18)}</a
      >
    </section>`}renderIntegration(e){let t=e.integration,n=ct(t),r=t?.entries[0],i=Object.entries(t?.entities_by_domain??{}).map(([e,t])=>`${t} ${e.replace(`_`,` `)}`).join(`, `);return E`<section class="card" data-testid="card-integration">
      <header class="card-head">
        ${N(`puzzle`)}
        <h3>Existing integration</h3>
        ${I(n.tone,n.label)}
      </header>
      <p class="muted">
        Smart Occupancy & Lighting custom integration (<code>${t?.domain??`smart_occupancy`}</code>).
        ${t?.detected?`It keeps running all automation. The App edits only its room configuration, through the integration's own validated commands.`:`When it is installed, the App shows its status here. Nothing is installed or changed automatically.`}
      </p>
      ${t?.detected?L([[`Version`,t.version],[`Configuration`,r?`${r.title??`—`} (${r.state??`unknown`})`:`Not set up`],[`Entities`,t.entity_count?`${t.entity_count} (${i})`:`None`]]):O}
      ${e.integration_entities.length?E`<details class="entities">
            <summary>Live entity states (${e.integration_entities.length})</summary>
            <ul class="entity-list">
              ${e.integration_entities.map(e=>E`<li data-entity=${e.entity_id}>
                  <span class="entity-main">
                    <span class="entity-name">${e.name}</span>
                    <span class="entity-id">${e.entity_id}</span>
                  </span>
                  <span class="entity-state">
                    <span class="state">${pt(e.state,e.unit)}</span>
                    <span class="muted small">${V(e.last_changed)}</span>
                  </span>
                </li>`)}
            </ul>
          </details>`:O}
    </section>`}renderModules(e){return E`<section class="card wide" data-testid="card-modules">
      <header class="card-head">
        ${N(`view-dashboard`)}
        <h3>Modules</h3>
      </header>
      <div class="tiles">
        ${e.modules.map(e=>E`<a
            class="tile"
            href="#/${e.key}"
            data-module=${e.key}
            @click=${t=>(t.preventDefault(),nt(e.key))}
          >
            <span class="tile-icon">${N(e.icon)}</span>
            <span class="tile-body">
              <span class="tile-title">${e.title}</span>
              ${e.status===`available`?I(`ok`,`Available`):e.status===`gui`?I(`info`,`Configured in the App`):I(`pending`,`Pending`)}
            </span>
          </a>`)}
      </div>
    </section>`}renderMigration(e){let t=e.migration;return E`<section class="card wide" data-testid="card-migration">
      <header class="card-head">
        ${N(`pending`)}
        <h3>Migration progress</h3>
        ${I(`info`,`${t.interfaces_migrated??0} of ${t.modules_total} configured in the App`)}
        ${I(t.modules_migrated===t.modules_total?`ok`:`pending`,`${t.modules_migrated} of ${t.modules_total} automations moved`)}
      </header>
      <div
        class="progress"
        role="progressbar"
        aria-label="Configuration interfaces in the App"
        aria-valuemin="0"
        aria-valuemax=${t.modules_total}
        aria-valuenow=${t.interfaces_migrated??0}
      >
        <span style="width: ${t.interface_percent??0}%"></span>
      </div>
      <p class="muted">
        Phase ${t.current_phase}: the Rooms configuration interface is in the App; the configuration itself stays in
        the integration, which also keeps running every automation. Modules move one at a time, only after you
        approve it. Each module's automation is run by exactly one system: today the integration runs
        ${t.controlled_by_integration} of ${t.modules_total}, the App ${t.controlled_by_app}.
      </p>
      <ol class="steps">
        ${t.items.map(e=>E`<li class="step step-${e.status}">
            <span class="step-mark" aria-hidden="true">${N(e.status===`done`?`ok`:`pending`,18)}</span>
            <span class="step-title"
              >${e.title}
              ${e.controlled_by?E`<span class="muted small block"
                    >Configured in: ${e.gui_owner===`app`?`App`:`integration`} · stored in:
                    ${e.config_source===`app`?`App`:`integration`} · automation:
                    ${e.controlled_by===`app`?`App`:`integration`}</span
                  >`:O}</span
            >
            ${e.status===`done`?I(`ok`,`Done`):e.status===`gui`?I(`info`,`Interface in App`):I(`pending`,`Pending`)}
          </li>`)}
      </ol>
    </section>`}render(){let e=p.overview;return E`
      ${R(`Dashboard`,`Status of the App, its Home Assistant connection and the migration.`)}
      <p class="notice notice-info" data-testid="readonly-notice">
        ${N(`shield-check`,20)}
        <span
          >Phase A2: Rooms can be configured here. Changes are reviewed before saving and stored in the Smart
          Occupancy integration, which keeps running every automation. The App never controls devices.</span
        >
      </p>
      ${e?E`<div class="grid">
            ${this.renderConnection(e)} ${this.renderApp(e)} ${this.renderIntegration(e)} ${this.renderModules(e)}
            ${this.renderMigration(e)}
          </div>`:lt()}
    `}};customElements.define(`sl-dashboard`,ht);var gt=6e4,U=null,_t=null;async function vt(e=!1){return!e&&U&&Date.now()-U.at<gt?U.data:(_t??=f(`api/lookup`).then(e=>(U={data:e,at:Date.now()},e)).finally(()=>{_t=null}),_t)}function W(e){return e.normalize(`NFKD`).replace(/\p{M}/gu,``).replace(/ð/g,`d`).replace(/Ð/g,`d`).replace(/[øØ]/g,`o`).replace(/[æÆ]/g,`ae`).toLowerCase()}function yt(e,t){let n=(...e)=>e.filter(Boolean).join(` · `);switch(t){case`area`:return e.areas.map(e=>({id:e.id,primary:e.name,secondary:`${e.device_count} devices · ${e.entity_count} entities`,search:W(`${e.name} ${e.id}`)}));case`device`:return e.devices.map(e=>({id:e.id,primary:e.name,secondary:n(e.area,e.manufacturer,e.model),search:W(n(e.name,e.area,e.manufacturer,e.model))}));default:return e.entities.map(e=>({id:e.entity_id,primary:e.name,secondary:n(e.entity_id,e.area),search:W(n(e.name,e.entity_id,e.area,e.device))}))}}function bt(e,t,n){let r=W(t).split(/\s+/).filter(Boolean);if(!r.length)return{items:e.slice(0,n),total:e.length};let i=e.filter(e=>r.every(t=>e.search.includes(t))),a=r[0];return i.sort((e,t)=>+!W(e.primary).startsWith(a)-!W(t.primary).startsWith(a)||e.primary.localeCompare(t.primary)),{items:i.slice(0,n),total:i.length}}var xt={entity:{title:`Choose an entity`,placeholder:`Choose an entity…`,search:`Search name, entity ID or area`},device:{title:`Choose a device`,placeholder:`Choose a device…`,search:`Search name, area or model`},area:{title:`Choose an area`,placeholder:`Choose an area…`,search:`Search areas`}},St=60,Ct=0,wt=class extends F{static{this.properties={kind:{type:String},value:{type:String},label:{type:String},open:{state:!0},query:{state:!0},active:{state:!0},all:{state:!0},error:{state:!0}}}constructor(){super(),this.uid=`picker-${++Ct}`,this.loadedKind=null,this.kind=`entity`,this.value=null,this.label=``,this.open=!1,this.query=``,this.active=0,this.all=null,this.error=null}get dialog(){return this.querySelector(`dialog`)}async load(e=!1){try{let t=await vt(e);this.all=yt(t,this.kind),this.loadedKind=this.kind,this.error=null}catch(e){this.error=`Could not load the list (${String(e)}).`}}willUpdate(e){e.has(`kind`)&&this.loadedKind&&this.loadedKind!==this.kind&&(this.all=null,this.load())}async show(){this.query=``,this.active=0,this.open=!0,await this.updateComplete,this.dialog?.showModal(),this.querySelector(`.picker-search input`)?.focus(),await this.load(!0)}close(){this.dialog?.close()}onClosed(){this.open=!1,this.querySelector(`.picker-field`)?.focus()}choose(e){this.value=e.id,this.dispatchEvent(new CustomEvent(`value-changed`,{detail:{kind:this.kind,value:e.id},bubbles:!0,composed:!0})),this.close()}onInput(e){this.query=e.target.value,this.active=0}onKey(e,t){if(e.key===`ArrowDown`||e.key===`ArrowUp`){e.preventDefault();let n=e.key===`ArrowDown`?1:-1;this.active=Math.max(0,Math.min(t.length-1,this.active+n)),this.querySelector(`#${this.uid}-opt-${this.active}`)?.scrollIntoView({block:`nearest`})}else e.key===`Enter`&&t[this.active]&&(e.preventDefault(),this.choose(t[this.active]))}selectedLabel(){if(!this.value)return null;let e=this.all?.find(e=>e.id===this.value);return e?e.primary:this.value}render(){let e=xt[this.kind]??xt.entity,t=this.all?bt(this.all,this.query,St):{items:[],total:0},n=t.items,r=this.selectedLabel(),i=`${this.uid}-list`;return E`
      <span class="field-label" id="${this.uid}-label">${this.label||e.title}</span>
      <button
        type="button"
        class="picker-field"
        aria-haspopup="dialog"
        aria-labelledby="${this.uid}-label ${this.uid}-value"
        @click=${()=>void this.show()}
        data-testid="picker-field"
      >
        <span id="${this.uid}-value" class=${r?``:`muted`}>${r??e.placeholder}</span>
        ${N(`chevron-right`,20)}
      </button>
      ${this.open?E`<dialog class="sheet sheet-full" aria-labelledby="${this.uid}-title" @close=${this.onClosed}
            @click=${e=>e.target===this.dialog&&this.close()}>
            <div class="sheet-inner">
              <header class="sheet-head">
                <h2 id="${this.uid}-title">${e.title}</h2>
                <button type="button" class="icon-btn" aria-label="Close" @click=${this.close}>${N(`close`)}</button>
              </header>
              <div class="picker-search">
                ${N(`search`,20)}
                <input
                  type="search"
                  inputmode="search"
                  enterkeyhint="search"
                  autocomplete="off"
                  autocapitalize="off"
                  spellcheck="false"
                  role="combobox"
                  aria-expanded="true"
                  aria-controls=${i}
                  aria-activedescendant=${n.length?`${this.uid}-opt-${this.active}`:O}
                  aria-label=${e.search}
                  placeholder=${e.search}
                  .value=${this.query}
                  @input=${this.onInput}
                  @keydown=${e=>this.onKey(e,n)}
                />
              </div>
              <div class="picker-results">
                ${this.error?E`<p class="notice notice-error">${this.error}</p>`:this.all?n.length?E`<ul id=${i} role="listbox" aria-label=${e.title}>
                          ${n.map((e,t)=>E`<li
                              id="${this.uid}-opt-${t}"
                              role="option"
                              aria-selected=${e.id===this.value?`true`:`false`}
                              class=${t===this.active?`active`:``}
                              data-value=${e.id}
                              @click=${()=>this.choose(e)}
                            >
                              <span class="opt-primary">${e.primary}</span>
                              <span class="opt-secondary">${e.secondary}</span>
                            </li>`)}
                        </ul>
                        ${t.total>n.length?E`<p class="muted small pad">
                              Showing ${n.length} of ${t.total}. Type more to narrow the list.
                            </p>`:O}`:E`<p class="muted pad">Nothing matches “${this.query}”.</p>`:E`<p class="muted pad">Loading…</p>`}
              </div>
            </div>
          </dialog>`:O}
    `}};customElements.define(`sl-picker`,wt);var Tt=[{value:`entity`,label:`Entity`},{value:`device`,label:`Device`},{value:`area`,label:`Area`}],Et=class extends F{static{this.properties={kind:{state:!0},selected:{state:!0},data:{state:!0}}}constructor(){super(),this.kind=`entity`,this.selected=null,this.data=null}setKind(e){this.kind=e,this.selected=null}async onPicked(e){this.selected=e.detail.value,this.data=await vt()}renderDetails(){let e=this.data;if(!e||!this.selected)return O;if(this.kind===`entity`){let t=e.entities.find(e=>e.entity_id===this.selected);return t?L([[`Name`,t.name],[`Entity ID`,E`<code>${t.entity_id}</code>`],[`State`,pt(t.state,t.unit)],[`Last changed`,V(t.last_changed)],[`Area`,t.area],[`Device`,t.device],[`Integration`,t.platform]]):O}if(this.kind===`device`){let t=e.devices.find(e=>e.id===this.selected);if(!t)return O;let n=e.entities.filter(e=>e.device_id===t.id);return E`${L([[`Name`,t.name],[`Area`,t.area],[`Manufacturer`,t.manufacturer],[`Model`,t.model],[`Entities`,String(n.length)]])}
        ${n.length?E`<ul class="plain-list">
              ${n.slice(0,30).map(e=>E`<li>${e.name} <span class="muted">· ${pt(e.state,e.unit)}</span></li>`)}
            </ul>`:O}`}let t=e.areas.find(e=>e.id===this.selected);return t?L([[`Name`,t.name],[`Area ID`,E`<code>${t.id}</code>`],[`Devices`,String(t.device_count)],[`Entities`,String(t.entity_count)]]):O}render(){return E`<section class="card wide" data-testid="card-lookup">
      <header class="card-head">${N(`search`)}<h3>Home Assistant lookup</h3></header>
      <p class="muted">Find an entity, device or area and see how Home Assistant reports it. Read-only.</p>
      <fieldset class="segmented" aria-label="Look up">
        <legend class="visually-hidden">Look up</legend>
        ${Tt.map(e=>E`<label class=${this.kind===e.value?`selected`:``}>
            <input type="radio" name="lookup-kind" value=${e.value} .checked=${this.kind===e.value} @change=${()=>this.setKind(e.value)} />
            ${e.label}
          </label>`)}
      </fieldset>
      <div class="lookup-body">
        <sl-picker .kind=${this.kind} .value=${this.selected} label="Find" @value-changed=${this.onPicked}></sl-picker>
        <div class="lookup-details" data-testid="lookup-details">
          ${this.selected?this.renderDetails():E`<p class="muted">Choose an item to see its details.</p>`}
        </div>
      </div>
    </section>`}};customElements.define(`sl-lookup-card`,Et);function G(e){return e==null||e===``?`—`:typeof e==`boolean`?e?`Yes`:`No`:String(e)}var Dt=class extends F{static{this.properties={details:{state:!0},loadingDetails:{state:!0},detailsError:{state:!0},showJson:{state:!0},copied:{state:!0}}}constructor(){super(),this.details=null,this.loadingDetails=!1,this.detailsError=null,this.showJson=!1,this.copied=null}async copyJson(){let e=JSON.stringify(this.details,null,2);try{await navigator.clipboard.writeText(e),this.copied=`Copied`}catch{let e=this.querySelector(`textarea.json-view`);e?.focus(),e?.select(),this.copied=`Selected: use your device's Copy`}window.setTimeout(()=>this.copied=null,4e3)}connectedCallback(){super.connectedCallback(),this.loadDetails()}async loadDetails(){this.loadingDetails=!0;try{this.details=await f(`api/diagnostics`),this.detailsError=null}catch(e){this.detailsError=String(e)}finally{this.loadingDetails=!1}}render(){let e=p.overview,t=E`<div class="actions">
      <button class="btn" @click=${()=>void this.loadDetails()} ?disabled=${this.loadingDetails}>
        ${N(`refresh`,18)} Refresh
      </button>
      <button class="btn" @click=${()=>this.showJson=!this.showJson} aria-expanded=${this.showJson?`true`:`false`} data-testid="view-diagnostics">
        ${N(`json`,18)} ${this.showJson?`Hide`:`View`} data
      </button>
      <a class="btn btn-primary" href=${d(`api/diagnostics?download=1`)} download data-testid="download-diagnostics">
        ${N(`download`,18)} Download
      </a>
    </div>`;if(!e)return E`${R(`Diagnostics`,`Health checks and connection details.`,t)} ${lt()}`;let n=e.home_assistant.connection,r=e.supervisor,i=this.details,a=z(p.link);return E`
      ${R(`Diagnostics`,`Health checks and connection details.`,t)}
      ${this.showJson&&i?E`<section class="card json-card" data-testid="card-json">
            <header class="card-head">${N(`json`)}<h3>Diagnostics data</h3>
              <button class="btn" @click=${()=>void this.copyJson()} data-testid="copy-diagnostics">${N(`copy`,18)} ${this.copied??`Copy`}</button>
            </header>
            <p class="muted small">Share this when reporting a problem. It contains no passwords or tokens.</p>
            <textarea class="json-view mono" readonly rows="14" aria-label="Diagnostics data">${JSON.stringify(i,null,2)}</textarea>
          </section>`:O}
      <div class="grid">
        <section class="card" data-testid="card-checks">
          <header class="card-head">${N(`stethoscope`)}<h3>Health checks</h3></header>
          <ul class="checks">
            ${e.checks.map(e=>E`<li data-check=${e.key} data-status=${e.status}>
                ${st(e.status)}
                <span><strong>${e.title}</strong><br /><span class="muted">${e.detail}</span></span>
              </li>`)}
          </ul>
        </section>

        <section class="card" data-testid="card-safety">
          <header class="card-head">${N(`shield-check`)}<h3>Safety</h3>${I(`ok`,`No device control`)}</header>
          ${L([[`Service calls sent`,String(e.safety.service_calls_sent)],[`Configuration changes`,e.safety.write_scope??`None`],[`Configuration commands sent`,String(e.safety.config_writes_sent??0)],[`Device control`,e.safety.device_control?`On`:`Off`],[`Automation engine`,e.safety.automation_engine?`Running`:`None (the integration stays in charge)`],[`Automatic migration`,e.safety.automatic_migration?`On`:`Off`],[`Changes to the integration`,e.safety.modifies_integration?`Room configuration (this session)`:`None`]])}
          <details>
            <summary>Home Assistant requests made</summary>
            <ul class="plain-list mono">
              ${Object.entries(e.safety.commands_sent).map(([e,t])=>E`<li>${e}: ${t}</li>`)}
            </ul>
            <p class="muted small">Allowed reads: ${e.safety.allowed_commands.join(`, `)}.</p>
            <p class="muted small">Allowed configuration commands: ${(e.safety.allowed_write_commands??[]).join(`, `)||`none`}.</p>
            ${(e.safety.recent_config_writes??[]).length?E`<ul class="plain-list mono">${(e.safety.recent_config_writes??[]).map(e=>E`<li>${e.time} ${e.type} ${e.area_id??``} → ${e.result??`…`}</li>`)}</ul>`:O}
          </details>
        </section>

        <section class="card" data-testid="card-ha-connection">
          <header class="card-head">${N(`home-assistant`)}<h3>Home Assistant connection</h3>
            ${I(B(n).tone,B(n).label)}</header>
          ${L([[`State`,n.state],[`Version`,n.ha_version],[`Connected since`,V(n.connected_since)],[`Last disconnect`,V(n.last_disconnect)],[`Reconnects`,n.reconnects],[`Response time`,n.latency_ms==null?null:`${n.latency_ms} ms`],[`State changes received`,n.events_total],[`Last error`,n.last_error],[`Browser link`,a.label]])}
        </section>

        <section class="card" data-testid="card-app-details">
          <header class="card-head">${N(`lightbulb-auto`)}<h3>App</h3></header>
          ${L([[`Version`,e.app.version],[`Phase`,e.app.phase],[`Started`,V(e.app.started_at)],[`Running for`,ft(e.app.uptime_s)],r?.available?[`Latest version`,G(r.app.version_latest)]:null,r?.available?[`Architecture`,G(r.app.arch)]:null,i?[`Python`,i.runtime.python]:null,i?[`aiohttp`,i.runtime.aiohttp]:null])}
        </section>

        <section class="card" data-testid="card-system">
          <header class="card-head">${N(`info`)}<h3>System</h3>
            ${r?.configured?I(r.available?`ok`:`warning`,r.available?`Supervisor`:`Unavailable`):I(`neutral`,`Development`)}</header>
          ${r?.available?L([[`Home Assistant`,G(r.system.homeassistant)],[`Supervisor`,G(r.system.supervisor)],[`Operating system`,G(r.system.operating_system)],[`Machine`,G(r.system.machine)],[`Update channel`,G(r.system.channel)],[`Supported / healthy`,`${G(r.system.supported)} / ${G(r.system.healthy)}`]]):E`<p class="muted">${r?.error??`Supervisor information is not available.`}</p>`}
        </section>

        <sl-lookup-card></sl-lookup-card>

        <section class="card wide" data-testid="card-log">
          <header class="card-head">${N(`stethoscope`)}<h3>Recent log</h3></header>
          ${this.detailsError?E`<p class="notice notice-error">Could not load details: ${this.detailsError}</p>`:O}
          <details>
            <summary>Show the last ${i?Math.min(i.log.length,100):0} log entries</summary>
            <div class="log mono">
              ${(i?.log??[]).slice(-100).reverse().map(e=>E`<div class="log-line log-${e.level}">
                  <span class="nowrap">${V(e.time)}</span> <span class="log-level">${e.level}</span>
                  <span>${e.message}</span>
                </div>`)}
            </div>
          </details>
        </section>
      </div>
    `}};customElements.define(`sl-diagnostics`,Dt);var Ot=class extends F{static{this.properties={moduleKey:{type:String}}}render(){let e=P.find(e=>e.key===this.moduleKey),t=p.overview?.modules.find(e=>e.key===this.moduleKey),n=t?.title??e?.title??this.moduleKey,r=p.overview?.integration,i=ct(r),a=r?.status===`loaded`;return E`
      ${R(n,t?.summary??``,I(`pending`,`Migration pending`))}
      <section class="card pending-card" data-testid="pending-${this.moduleKey}">
        <div class="pending-hero">
          <span class="pending-icon">${N(t?.icon??e?.icon??`puzzle`,40)}</span>
          <div>
            <h3>${n} has not been migrated to the App yet</h3>
            <p class="muted">
              This screen has no settings yet, and nothing on it can change your setup. The module moves to the App
              in a later phase, and only after you approve it.
            </p>
          </div>
        </div>
        <dl class="kv">
          <dt>Will cover</dt>
          <dd>${t?.summary??`—`}</dd>
          <dt>Configure it today in</dt>
          <dd>${t?.handled_by??`The existing integration's dashboard cards.`}</dd>
          <dt>In control</dt>
          <dd data-testid="controlled-by">
            ${t?.controlled_by===`app`?I(`ok`,`This App`):E`${I(`info`,`Smart Occupancy integration`)} ${I(i.tone,i.label)}`}
            ${a&&t?.controlled_by!==`app`?E`<span class="muted block">It keeps handling ${n.toLowerCase()} unchanged. Only one system
                  controls these devices at a time.</span>`:O}
          </dd>
        </dl>
      </section>
    `}};customElements.define(`sl-pending-module`,Ot);var K=class extends Error{constructor(e,t,n){super(e),this.code=t,this.status=n}};async function kt(e){let t=null;try{t=await e.json()}catch{}if(!e.ok){let n=t??{};throw new K(n.error??`HTTP ${e.status}`,n.code??`http_error`,e.status)}return t}async function q(e){let t;try{t=await fetch(d(e),{cache:`no-store`,credentials:`same-origin`})}catch(e){throw new K(`The App cannot be reached (${String(e)})`,`network`,0)}return kt(t)}async function J(e,t){let n;try{n=await fetch(d(e),{method:`POST`,cache:`no-store`,credentials:`same-origin`,headers:{"Content-Type":`application/json`,"X-Smart-Lighting":`1`},body:JSON.stringify(t)})}catch(e){throw new K(`The App could not be reached (${String(e)}). If you were saving, the change may or may not have been stored: reload the room to check.`,`network`,0)}return kt(n)}function At(e,t){let n=new Blob([JSON.stringify(e,null,1)],{type:`application/json`}),r=URL.createObjectURL(n),i=document.createElement(`a`);i.href=r,i.download=t,document.body.append(i),i.click(),i.remove(),window.setTimeout(()=>URL.revokeObjectURL(r),1e3)}function Y(e){return{enabled:!!e.enabled,sensors:(e.sensors??[]).map(e=>({entity_id:e.entity_id,kind:e.kind,role:e.role,inverted:!!e.inverted,enabled:e.enabled!==!1})),lights:[...e.lights??[]],reference_occupancy_entity:e.reference_occupancy_entity??null,adjacent_rooms:[...e.adjacent_rooms??[]]}}function jt(e){return{enabled:e.enabled,sensors:e.sensors.map(e=>({entity_id:e.entity_id,kind:e.kind,role:e.role,inverted:!!e.inverted,enabled:e.enabled!==!1})),lights:e.lights,reference_occupancy_entity:e.reference_occupancy_entity||null,adjacent_rooms:e.adjacent_rooms}}function Mt(e,t,n){let r=[];!n&&(t.enabled||t.sensors.length||t.lights.length)&&r.push({type:`room_added`}),e.enabled!==t.enabled&&(n||t.enabled)&&r.push({type:t.enabled?`room_enabled`:`room_disabled`});let i=new Map(e.sensors.map(e=>[e.entity_id,e])),a=new Map(t.sensors.map(e=>[e.entity_id,e]));for(let[e,t]of a)i.has(e)||r.push({type:`sensor_added`,entity_id:e,kind:t.kind,role:t.role});for(let[e,t]of i)a.has(e)||r.push({type:`sensor_removed`,entity_id:e,kind:t.kind,role:t.role});for(let[e,t]of a){let n=i.get(e);n&&(n.kind!==t.kind&&r.push({type:`sensor_kind`,entity_id:e,old:n.kind,new:t.kind}),n.role!==t.role&&r.push({type:`sensor_role`,entity_id:e,old:n.role,new:t.role}),!!n.inverted!=!!t.inverted&&r.push({type:`sensor_inverted`,entity_id:e,old:!!n.inverted,new:!!t.inverted}),n.enabled!==!1!=(t.enabled!==!1)&&r.push({type:`sensor_enabled`,entity_id:e,old:n.enabled!==!1,new:t.enabled!==!1}))}for(let n of t.lights)e.lights.includes(n)||r.push({type:`light_added`,entity_id:n});for(let n of e.lights)t.lights.includes(n)||r.push({type:`light_removed`,entity_id:n});(e.reference_occupancy_entity??null)!==(t.reference_occupancy_entity??null)&&r.push({type:`reference_changed`,old:e.reference_occupancy_entity,new:t.reference_occupancy_entity});for(let n of t.adjacent_rooms)e.adjacent_rooms.includes(n)||r.push({type:`adjacent_added`,area_id:n});for(let n of e.adjacent_rooms)t.adjacent_rooms.includes(n)||r.push({type:`adjacent_removed`,area_id:n});return r}function Nt(e,t){return e?.kinds.find(e=>e.value===t)?.default_role??(t===`pir`||t===`door`?`trigger`:t===`other`?`ignore`:`maintain`)}function Pt(e,t,n){let r=e?.kinds.find(e=>e.value===t)?.domains;return!r||r.includes(n.split(`.`)[0])}function X(e,t,n){return(e?.kinds??[]).filter(n=>Pt(e,n.value,t)).sort((e,t)=>e.value===n?-1:+(t.value===n))}var Z={ok:{tone:`ok`,label:`Online`,help:`The entity reports a state.`},unknown:{tone:`neutral`,label:`No value yet`,help:`The entity exists but has not reported a value since Home Assistant started. Battery devices often report only on their next change.`},unavailable:{tone:`warning`,label:`Entity unavailable`,help:`Home Assistant reports this entity as unavailable, while other entities of its device (if any) still respond.`},device_offline:{tone:`error`,label:`Device offline`,help:`Every entity of this physical device is unavailable: the device is unreachable (power, battery, radio or its integration).`},not_provided:{tone:`error`,label:`Not provided`,help:`The entity is registered, but its integration does not provide it (it failed to load, or the device was removed from it).`},disabled:{tone:`warning`,label:`Disabled`,help:`The entity is disabled in Home Assistant, so it has no state.`},missing:{tone:`error`,label:`Missing`,help:`The entity does not exist in Home Assistant (deleted, or renamed while the integration was not running).`}},Q={configured:{tone:`ok`,label:`Configured`},not_configured:{tone:`neutral`,label:`Not configured`},disabled:{tone:`pending`,label:`Disabled`},problem:{tone:`error`,label:`Problem`}},Ft={confirmed_occupancy:`Occupied`,possible_occupancy:`Possibly occupied`,vacant:`Vacant`,unknown:`Unknown`};function It(e,t,n,r,i){switch(e.type){case`room_added`:return`Room configuration is created`;case`room_removed`:return`Room configuration is removed`;case`room_enabled`:return`Room is enabled (occupancy is observed)`;case`room_disabled`:return`Room is disabled (occupancy is no longer observed)`;case`sensor_added`:return`${t(e.entity_id)} added as ${r(e.kind)} · ${i(e.role)}`;case`sensor_removed`:return`${t(e.entity_id)} removed (was ${r(e.kind)})`;case`sensor_kind`:return`${t(e.entity_id)}: type ${r(e.old)} → ${r(e.new)}`;case`sensor_role`:return`${t(e.entity_id)}: role ${i(e.old)} → ${i(e.new)}`;case`sensor_inverted`:return`${t(e.entity_id)}: ${e.new?`inverted (active when off)`:`no longer inverted`}`;case`sensor_enabled`:return`${t(e.entity_id)}: ${e.new?`included in occupancy`:`excluded from occupancy`}`;case`light_added`:return`${t(e.entity_id)} added as a light`;case`light_removed`:return`${t(e.entity_id)} removed from the lights`;case`reference_changed`:return`Reference occupancy entity: ${e.old?t(String(e.old)):`none`} → ${e.new?t(String(e.new)):`none`}`;case`adjacent_added`:return`${n(e.area_id)} added as an adjacent room`;case`adjacent_removed`:return`${n(e.area_id)} removed from the adjacent rooms`;case`normalized`:return`Stored format is normalized (no functional change)`;default:return e.type}}function Lt(e){return e===`sensor_added`||e===`light_added`||e===`room_added`?`added`:e===`sensor_removed`||e===`light_removed`||e===`room_removed`?`removed`:e===`sensor_kind`||e===`sensor_role`?`classification`:e.startsWith(`adjacent`)||e===`reference_changed`?`relations`:`settings`}function Rt(e,t,n){let r=Y(n);t.enabled!==e.enabled&&(r.enabled=t.enabled);let i=new Map(e.sensors.map(e=>[e.entity_id,e])),a=new Map(t.sensors.map(e=>[e.entity_id,e]));r.sensors=r.sensors.filter(e=>!(i.has(e.entity_id)&&!a.has(e.entity_id)));for(let[e,t]of a){let n=i.get(e),a=r.sensors.find(t=>t.entity_id===e);if(!n)a||r.sensors.push({...t});else if(a)for(let e of[`kind`,`role`,`inverted`,`enabled`])(n[e]??null)!==(t[e]??null)&&(a[e]=t[e])}let o=e.lights.filter(e=>!t.lights.includes(e)),s=t.lights.filter(t=>!e.lights.includes(t));r.lights=[...r.lights.filter(e=>!o.includes(e)),...s.filter(e=>!r.lights.includes(e))],(t.reference_occupancy_entity??null)!==(e.reference_occupancy_entity??null)&&(r.reference_occupancy_entity=t.reference_occupancy_entity);let c=e.adjacent_rooms.filter(e=>!t.adjacent_rooms.includes(e)),l=t.adjacent_rooms.filter(t=>!e.adjacent_rooms.includes(t));return r.adjacent_rooms=[...r.adjacent_rooms.filter(e=>!c.includes(e)),...l.filter(e=>!r.adjacent_rooms.includes(e))],r}var zt=[{value:`all`,label:`All`},{value:`configured`,label:`Configured`},{value:`not_configured`,label:`Not configured`},{value:`disabled`,label:`Disabled`},{value:`problem`,label:`Problem`}],Bt=[{value:`name`,label:`Name`},{value:`status`,label:`Status`},{value:`occupancy`,label:`Occupancy`},{value:`attention`,label:`Needs attention`}],Vt={problem:0,configured:1,disabled:2,not_configured:3};function Ht(e){let t=(e??``).replace(/^mdi:/,``);return t&&Ze[t]?t:`sofa`}function Ut(e){if(!e||!e.state)return I(`neutral`,`No occupancy data`);let t=Ft[e.state]??e.state;return I(e.occupied?`ok`:e.state===`possible_occupancy`?`info`:`neutral`,t)}function Wt(e){return e?e.api===`unsupported`?E`<p class="notice notice-warning" data-testid="rooms-api-notice">${N(`warning`,20)}<span>${e.error} Update Smart Occupancy &
        Lighting (HACS) to edit rooms here; everything below is read from Home Assistant.</span></p>`:e.api===`error`||e.api===`unavailable`?E`<p class="notice notice-warning" data-testid="rooms-api-notice">${N(`warning`,20)}<span>The integration's room configuration
        could not be read: ${e.error}</span></p>`:e.loaded?O:E`<p class="notice notice-warning" data-testid="rooms-api-notice">${N(`warning`,20)}<span>Smart Occupancy & Lighting is not
        running (state: ${e.integration_state??`unknown`}). Rooms are shown from its stored configuration; saving is
        possible again once it runs.</span></p>`:O}var Gt=class extends F{static{this.properties={data:{state:!0},error:{state:!0},query:{state:!0},filter:{state:!0},sort:{state:!0},backupsOpen:{state:!0}}}constructor(){super(),this.seenRooms=-1,this.seenRegistries=-1,this.loading=!1,this.data=null,this.error=null,this.query=``,this.filter=`all`,this.sort=`name`,this.backupsOpen=!1}connectedCallback(){super.connectedCallback(),this.load()}async load(){if(!this.loading){this.loading=!0,this.seenRooms=p.roomsVersion,this.seenRegistries=p.registriesVersion;try{this.data=await q(`api/rooms`),this.error=null}catch(e){this.error=e instanceof K?e.message:String(e)}finally{this.loading=!1}}}willUpdate(){this.data&&(p.roomsVersion!==this.seenRooms||p.registriesVersion!==this.seenRegistries)&&this.load()}visible(){let e=W(this.query).split(/\s+/).filter(Boolean),t=(this.data?.rooms??[]).filter(t=>{if(this.filter!==`all`&&t.status!==this.filter)return!1;if(!e.length)return!0;let n=W([t.name,t.area_id,...t.sensor_ids,...t.light_ids].join(` `));return e.every(e=>n.includes(e))}),n=e=>e.warnings.filter(e=>e.level!==`info`).length,r=e=>e.occupancy?.occupied?0:e.occupancy?.state===`possible_occupancy`?1:e.occupancy?2:3,i=(e,t)=>e.name.localeCompare(t.name);return t.sort((e,t)=>this.sort===`status`?Vt[e.status]-Vt[t.status]||i(e,t):this.sort===`occupancy`?r(e)-r(t)||i(e,t):this.sort===`attention`&&n(t)-n(e)||i(e,t))}open(e,t){t.button!==0||t.metaKey||t.ctrlKey||t.shiftKey||(t.preventDefault(),rt(`rooms`,e.area_id))}renderCard(e){let t=Q[e.status],n=e.warnings.filter(e=>e.level!==`info`),r=e.devices,i=e.automation.lighting;return E`<a
      class="room-card card status-${e.status}"
      href="#/rooms/${encodeURIComponent(e.area_id)}"
      data-room=${e.area_id}
      @click=${t=>this.open(e,t)}
    >
      <header class="room-card-head">
        <span class="room-icon">${N(Ht(e.icon),22)}</span>
        <span class="room-title">
          <span class="room-name">${e.name}</span>
          ${e.area_exists?O:E`<span class="small tone-error">Area deleted</span>`}
        </span>
        ${I(t.tone,t.label)}
      </header>
      <div class="room-occupancy">${e.configured&&e.enabled?Ut(e.occupancy):E`<span class="muted small">Occupancy not observed</span>`}
        ${e.occupancy?.reason?E`<span class="muted small room-reason">${e.occupancy.reason}</span>`:O}</div>
      <dl class="room-stats">
        <div><dt>${N(`motion-sensor`,18)}<span class="visually-hidden">Sensors</span></dt><dd>${H(e.sensors,`sensor`,`sensors`)}</dd></div>
        <div><dt>${N(`lightbulb`,18)}<span class="visually-hidden">Lights</span></dt><dd>${H(e.lights,`light`,`lights`)}</dd></div>
        <div title="Devices in this room and of its assigned entities">
          <dt>${N(`home`,18)}<span class="visually-hidden">Devices</span></dt>
          <dd>
            <span class=${r.online?`tone-ok`:`muted`}>${r.online} online</span>${r.offline?E` · <span class="tone-error">${r.offline} offline</span>`:O}${r.partial||r.unavailable_entities?E` · <span class="tone-warning">${r.unavailable_entities} unavailable</span>`:O}${r.missing_entities?E` · <span class="tone-error">${r.missing_entities} missing</span>`:O}
          </dd>
        </div>
      </dl>
      <p class="room-owner small muted">
        ${N(i===`automatic`?`robot`:`observe`,16)}
        <span
          >${e.configured?E`Automation: Smart Occupancy integration${e.automation.running?``:` (not running)`} · lighting
              ${i??`unknown`}`:e.unassigned_candidates?E`${H(e.unassigned_candidates,`sensor or light`,`sensors or lights`)} found in this area`:E`No sensors or lights found in this area`}</span
        >
      </p>
      ${n.length?E`<ul class="room-warnings">
            ${n.slice(0,2).map(e=>E`<li class="tone-${e.level===`error`?`error`:`warning`}">${N(e.level===`error`?`error`:`warning`,16)}<span>${e.title}${e.entity_id?E` · <span class="muted">${e.entity_id}</span>`:O}</span></li>`)}
            ${n.length>2?E`<li class="muted">+ ${n.length-2} more</li>`:O}
          </ul>`:O}
    </a>`}render(){let e=this.data,t=e?this.visible():[],n=new Map([[`all`,e?.rooms.length??0]]);for(let t of e?.rooms??[])n.set(t.status,(n.get(t.status)??0)+1);return E`
      ${R(`Rooms`,`Every Home Assistant area, its devices and its room configuration. Changes are saved in the Smart Occupancy integration, which keeps running all automation.`,E`<div class="actions">
          <button type="button" class="btn" @click=${()=>void this.load()} aria-label="Refresh">${N(`refresh`,20)}<span>Refresh</span></button>
          <button type="button" class="btn" data-testid="open-backups" @click=${()=>this.backupsOpen=!0}>
            ${N(`history`,20)}<span>Backups</span>
          </button>
        </div>`)}
      ${this.error?E`<p class="notice notice-error" role="alert">${N(`error`,20)}<span>${this.error}</span></p>`:O}
      ${Wt(e?.status)}
      <div class="rooms-toolbar">
        <label class="search-field">
          ${N(`search`,20)}
          <input type="search" placeholder="Search rooms, sensors or lights" aria-label="Search rooms" .value=${this.query}
            @input=${e=>this.query=e.target.value} />
        </label>
        <fieldset class="segmented" aria-label="Filter by status">
          <legend class="visually-hidden">Filter by status</legend>
          ${zt.map(e=>E`<label class=${this.filter===e.value?`selected`:``}>
              <input type="radio" name="room-filter" .checked=${this.filter===e.value} @change=${()=>this.filter=e.value} />
              ${e.label} <span class="count">${n.get(e.value)??0}</span>
            </label>`)}
        </fieldset>
        <label class="select-field">
          <span>Sort</span>
          <select aria-label="Sort rooms" .value=${this.sort} @change=${e=>this.sort=e.target.value}>
            ${Bt.map(e=>E`<option value=${e.value} ?selected=${this.sort===e.value}>${e.label}</option>`)}
          </select>
        </label>
      </div>
      ${!e&&!this.error?E`<div class="card loading" role="status"><span class="spinner" aria-hidden="true"></span><span>Loading rooms…</span></div>`:O}
      ${e&&!t.length?E`<div class="card empty-state">
            ${N(`floor-plan`,40)}
            <h3>${e.rooms.length?`No room matches`:`No areas in Home Assistant yet`}</h3>
            <p class="muted">
              ${e.rooms.length?`Change the search or the status filter.`:`Create areas under Settings → Areas, labels & zones and put your devices in them. They appear here automatically.`}
            </p>
          </div>`:O}
      <div class="room-grid" data-testid="room-grid">${t.map(e=>this.renderCard(e))}</div>
      ${e?E`<p class="muted small">Read ${V(e.generated_at)} · ${e.status.integration_version?`integration ${e.status.integration_version}`:`integration version unknown`}</p>`:O}
      ${this.backupsOpen?E`<sl-room-backups @close=${()=>(this.backupsOpen=!1,void this.load())}></sl-room-backups>`:O}
    `}};customElements.define(`sl-rooms`,Gt);var Kt=class extends F{static{this.properties={backups:{state:!0},review:{state:!0},pending:{state:!0},confirmed:{state:!0},error:{state:!0},done:{state:!0},busy:{state:!0}}}constructor(){super(),this.backups=null,this.review=null,this.pending=null,this.confirmed=new Set,this.error=null,this.done=null,this.busy=!1}async connectedCallback(){super.connectedCallback(),await this.updateComplete,this.querySelector(`dialog`)?.showModal();try{this.backups=(await q(`api/rooms/backups`)).backups}catch(e){this.error=String(e),this.backups=[]}}close(){this.querySelector(`dialog`)?.close()}async export(){try{let e=await q(`api/rooms/export`);At(e,`smart-occupancy-rooms-${e.created_at.replace(/[:-]/g,``).slice(0,15)}.json`)}catch(e){this.error=String(e)}}async download(e){try{At(await q(`api/rooms/backups/${encodeURIComponent(e)}`),`smart-occupancy-${e}.json`)}catch(e){this.error=String(e)}}async reviewBackup(e){this.error=null,this.done=null,this.busy=!0;try{this.review=await J(`api/rooms/restore/review`,{backup:e}),this.pending=e,this.confirmed=new Set}catch(e){this.error=e instanceof Error?e.message:String(e)}finally{this.busy=!1}}async fromStored(e){try{await this.reviewBackup(await q(`api/rooms/backups/${encodeURIComponent(e)}`))}catch(e){this.error=String(e)}}async fromFile(e){let t=e.target.files?.[0];if(t)try{let e=JSON.parse(await t.text());await this.reviewBackup(e)}catch{this.error=`This file is not a valid room configuration backup (JSON).`}}async restore(){if(this.review&&this.pending){this.busy=!0,this.error=null;try{await J(`api/rooms/restore`,{backup:this.pending,revision:this.review.revision,confirmed:[...this.confirmed]}),this.done=`Restored. ${H(this.review.rooms.length,`room was`,`rooms were`)} replaced; the integration reloads. The configuration before the restore was kept as a backup.`,this.review=null,this.pending=null,this.backups=(await q(`api/rooms/backups`)).backups}catch(e){this.error=e instanceof K&&e.code===`stale_revision`?`The room configuration changed after the review. Review the backup again.`:e instanceof Error?e.message:String(e),e instanceof K&&e.code===`stale_revision`&&(this.review=null)}finally{this.busy=!1}}}renderReview(e){let t=e.requires_confirmation,n=!e.errors.length&&t.every(e=>this.confirmed.has(e))&&e.writable&&!e.stale;return E`<section class="review-block" data-testid="restore-review">
      <h3>Review the restore</h3>
      ${e.rooms.length?O:E`<p class="muted">The backup is identical to the current configuration. Nothing to restore.</p>`}
      <ul class="change-list">
        ${e.rooms.map(e=>E`<li>
            <strong>${e.name??e.area_id}</strong> · ${H(e.changes.length,`change`,`changes`)}
            ${e.area_exists?O:I(`warning`,`Area no longer exists`)}
            ${e.missing_entities.length?E`<span class="small tone-warning block">Missing entities: ${e.missing_entities.join(`, `)}</span>`:O}
          </li>`)}
      </ul>
      ${e.errors.map(e=>E`<p class="notice notice-error">${N(`error`,20)}<span>${e.message}</span></p>`)}
      ${e.impacts.map(e=>e.level===`confirm`?E`<label class="confirm-row"><input type="checkbox" .checked=${this.confirmed.has(e.code)} @change=${t=>{let n=new Set(this.confirmed);t.target.checked?n.add(e.code):n.delete(e.code),this.confirmed=n}} /><span><strong>${e.title}</strong><span class="muted block">${e.detail}</span></span></label>`:E`<p class="notice notice-info">${N(`info`,20)}<span><strong>${e.title}</strong> ${e.detail}</span></p>`)}
      <div class="actions">
        <button type="button" class="btn" @click=${()=>(this.review=null,this.pending=null)}>Cancel</button>
        <button type="button" class="btn btn-primary" ?disabled=${!n||this.busy||!e.rooms.length} data-testid="restore-confirm" @click=${()=>void this.restore()}>
          ${N(`restore`,20)}Restore
        </button>
      </div>
    </section>`}render(){return E`<dialog class="sheet sheet-full backups-dialog" aria-labelledby="backups-title"
        @close=${()=>this.dispatchEvent(new CustomEvent(`close`))}
        @click=${e=>e.target.matches(`dialog`)&&this.close()}>
      <div class="sheet-inner">
        <header class="sheet-head">
          <h2 id="backups-title">Room configuration backups</h2>
          <button type="button" class="icon-btn" aria-label="Close" @click=${()=>this.close()}>${N(`close`)}</button>
        </header>
        <div class="sheet-body">
          <p class="muted">
            The App keeps the room configuration as it was before each change it saves (the latest 30). You can download
            the current configuration, or restore a backup after reviewing exactly what changes.
          </p>
          <div class="actions">
            <button type="button" class="btn" data-testid="export-rooms" @click=${()=>void this.export()}>${N(`download`,20)}Export current configuration</button>
            <label class="btn file-btn">${N(`file-upload`,20)}Restore from a file…<input type="file" accept="application/json,.json" @change=${e=>void this.fromFile(e)} /></label>
          </div>
          ${this.error?E`<p class="notice notice-error" role="alert">${N(`error`,20)}<span>${this.error}</span></p>`:O}
          ${this.done?E`<p class="notice notice-info" role="status">${N(`ok`,20)}<span>${this.done}</span></p>`:O}
          ${this.review?this.renderReview(this.review):O}
          <h3 class="section-title">Automatic backups</h3>
          ${this.backups===null?E`<p class="muted">Loading…</p>`:this.backups.length?E`<ul class="backup-list">
                  ${this.backups.map(e=>E`<li>
                      <span><strong>${V(e.created_at)}</strong> · ${e.reason??`Backup`}<span class="muted small block">${H(e.rooms,`room`,`rooms`)} · revision ${e.revision??`—`}</span></span>
                      <span class="row-actions">
                        <button type="button" class="btn" @click=${()=>void this.download(e.id)}>${N(`download`,18)}Download</button>
                        <button type="button" class="btn" @click=${()=>void this.fromStored(e.id)}>${N(`restore`,18)}Restore…</button>
                      </span>
                    </li>`)}
                </ul>`:E`<p class="muted">No backups yet. One is made before every change saved from this App.</p>`}
        </div>
      </div>
    </dialog>`}};customElements.define(`sl-room-backups`,Kt);var qt=[{key:`overview`,title:`Overview`,icon:`view-dashboard`},{key:`devices`,title:`Devices`,icon:`home`},{key:`presence`,title:`Presence`,icon:`motion-sensor`},{key:`lighting`,title:`Lighting`,icon:`lightbulb-group`},{key:`switches`,title:`Switches`,icon:`light-switch`},{key:`curtains`,title:`Curtains`,icon:`curtains`},{key:`diagnostics`,title:`Diagnostics`,icon:`stethoscope`}],Jt=[{key:`presence`,title:`Presence and contact sensors`},{key:`climate`,title:`Temperature and light level`},{key:`lighting`,title:`Lights and lighting outputs`},{key:`switches`,title:`Wall switches and remotes`},{key:`curtains`,title:`Curtains and covers`},{key:`other`,title:`Other`}],Yt=new Set([`pir`,`presence`,`door`,`window`,`pc_activity`,`bed`,`presence_input`,`illuminance`,`temperature`]),Xt={trigger:`Starts occupancy when it becomes active.`,maintain:`Keeps the room occupied while active (cannot start it alone after a quiet period).`,confirm_vacancy:`Holds occupancy; when it clears, vacancy is confirmed faster.`,ignore:`Shown, but not used for occupancy.`},Zt={active:`Active now`,clear:`Clear`,unavailable:`Unavailable`,unknown:`No value`,excluded:`Excluded`,ignored:`Ignored`,invalid:`State not understood`,stuck:`Stuck`,missing:`Missing`};function $(e){let t=Z[e]??{tone:`neutral`,label:e,help:``};return E`<span class="has-tip" title=${t.help}>${I(t.tone,t.label)}</span>`}var Qt=class extends F{static{this.properties={areaId:{type:String},tab:{type:String},ws:{state:!0},draft:{state:!0},removeRoom:{state:!0},selected:{state:!0},leftOpen:{state:!0},inspectorOpen:{state:!0},loadError:{state:!0},review:{state:!0},reviewOpen:{state:!0},reviewError:{state:!0},confirmed:{state:!0},busy:{state:!0},notice:{state:!0},changedElsewhere:{state:!0},pick:{state:!0},rooms:{state:!0},deviceFilter:{state:!0},deviceQuery:{state:!0},roomQuery:{state:!0}}}constructor(){super(),this.base=null,this.extra=new Map,this.loadedArea=null,this.seenRooms=-1,this.seenRegistries=-1,this.loading=!1,this.onBeforeUnload=e=>{this.dirty()&&(e.preventDefault(),e.returnValue=``)},this.kindLabel=e=>this.ws?.schema?.kinds.find(t=>t.value===e)?.label??String(e??`—`),this.roleLabel=e=>(this.ws?.schema?.roles.find(t=>t.value===e)?.label??String(e??`—`)).split(`:`)[0],this.areaId=``,this.tab=`overview`,this.ws=null,this.draft=null,this.removeRoom=!1,this.selected=null,this.leftOpen=window.innerWidth>=1280,this.inspectorOpen=!1,this.loadError=null,this.review=null,this.reviewOpen=!1,this.reviewError=null,this.confirmed=new Set,this.busy=!1,this.notice=null,this.changedElsewhere=!1,this.pick=null,this.rooms=null,this.deviceFilter=`all`,this.deviceQuery=``,this.roomQuery=``}connectedCallback(){super.connectedCallback(),window.addEventListener(`beforeunload`,this.onBeforeUnload),at(()=>this.dirty()?`${this.ws?.area.name??`This room`} has unsaved changes. Leave without saving them?`:null),this.loadRooms()}disconnectedCallback(){super.disconnectedCallback(),window.removeEventListener(`beforeunload`,this.onBeforeUnload),at(null),m.watch([])}willUpdate(e){e.has(`areaId`)&&this.areaId&&this.areaId!==this.loadedArea&&(this.reset(),this.load(!1)),this.ws&&!this.loading&&!this.busy&&(p.roomsVersion===this.seenRooms?p.registriesVersion!==this.seenRegistries&&(this.seenRegistries=p.registriesVersion,this.load(!0)):(this.seenRooms=p.roomsVersion,this.onRoomsChanged()))}reset(){this.ws=null,this.draft=null,this.base=null,this.removeRoom=!1,this.selected=null,this.inspectorOpen=!1,this.review=null,this.reviewOpen=!1,this.notice=null,this.changedElsewhere=!1,this.extra.clear()}async loadRooms(){try{this.rooms=(await q(`api/rooms`)).rooms}catch{this.rooms=null}}async load(e,t=!1){let n=this.areaId;this.loading=!0,this.seenRooms=p.roomsVersion,this.seenRegistries=p.registriesVersion;try{let r=await q(`api/rooms/${encodeURIComponent(n)}${t?`?refresh=1`:``}`);if(n!==this.areaId)return;this.applyWorkspace(r,e),this.loadError=null}catch(e){this.loadError=e instanceof K?e.message:String(e)}finally{this.loading=!1}}applyWorkspace(e,t){let n=this.dirty(),r=this.base;this.ws=e,this.loadedArea=e.area.id,this.base=Y(e.room.config),!t||!n||!this.draft||!r?(this.draft=Y(e.room.config),this.removeRoom=!1):this.removeRoom||(this.draft=Rt(r,this.draft,this.base));for(let t of Object.keys(e.entities))this.extra.delete(t);m.watch(Object.keys(e.entities).concat([...this.extra.keys()]))}async onRoomsChanged(){if(this.loadRooms(),!this.ws)return;let e=this.ws.room.revision;if(this.dirty())try{let t=await q(`api/rooms/${encodeURIComponent(this.areaId)}`);t.room.revision===e?this.applyWorkspace(t,!0):this.changedElsewhere=!0}catch{}else await this.load(!1)}changes(){return!this.base||!this.draft||!this.ws?[]:this.removeRoom?this.ws.room.configured?[{type:`room_removed`}]:[]:Mt(this.base,this.draft,this.ws.room.configured)}dirty(){return this.changes().length>0}update_(e){if(!this.draft)return;let t={...this.draft,sensors:this.draft.sensors.map(e=>({...e})),lights:[...this.draft.lights],adjacent_rooms:[...this.draft.adjacent_rooms]};e(t),this.draft=t,this.removeRoom=!1,this.notice=null}entity(e){let t=this.ws?.entities[e]??this.extra.get(e),n=p.liveStates.get(e),r=t??{entity_id:e,name:e,domain:e.split(`.`)[0],category:`other`,category_label:`Other`,category_confidence:`low`,category_reason:`Not known to Home Assistant`,suggested_kind:`other`,device_class:null,unit:null,icon:null,state:null,last_changed:null,last_updated:null,availability:`missing`,capabilities:[],platform:null,registered:!1,hidden:!1,entity_category:null,device_id:null,area_id:null,area:null,sensor_rooms:[],light_rooms:[],own:!1};return n?{...r,state:n.state,last_changed:n.last_changed,last_updated:n.last_updated,availability:n.availability}:r}name(e){return e?this.entity(e).name:`—`}areaName(e){return e?this.ws?.areas.find(t=>t.id===e)?.name??this.ws?.other_rooms[e]?.name??e:`—`}assignment(e){let t=this.draft;if(!t)return null;let n=t.sensors.find(t=>t.entity_id===e);return n?{kind:`sensor`,sensor:n}:t.lights.includes(e)?{kind:`light`}:t.reference_occupancy_entity===e?{kind:`reference`}:null}assignSensor(e,t){let n=this.entity(e),r=this.ws?.schema??null,i=X(r,e,n.suggested_kind),a=t??(i.find(e=>e.value===n.suggested_kind)??i[0])?.value??`other`;this.update_(t=>{t.lights=t.lights.filter(t=>t!==e),t.sensors.some(t=>t.entity_id===e)||t.sensors.push({entity_id:e,kind:a,role:Nt(r,a),inverted:!1,enabled:!0})}),this.select(e)}assignLight(e){this.update_(t=>{t.sensors=t.sensors.filter(t=>t.entity_id!==e),t.lights.includes(e)||t.lights.push(e)}),this.select(e)}unassign(e){this.update_(t=>{t.sensors=t.sensors.filter(t=>t.entity_id!==e),t.lights=t.lights.filter(t=>t!==e),t.reference_occupancy_entity===e&&(t.reference_occupancy_entity=null)})}setSensor(e,t){let n=this.ws?.schema??null;this.update_(r=>{let i=r.sensors.find(t=>t.entity_id===e);if(i){if(t.kind&&t.kind!==i.kind){let r=this.base?.sensors.find(t=>t.entity_id===e);i.role=r&&r.kind===t.kind?r.role:Nt(n,t.kind)}Object.assign(i,t)}})}discard(){this.base&&(this.draft=Y({...this.base}),this.removeRoom=!1,this.changedElsewhere=!1)}select(e){this.selected=e,this.inspectorOpen=e!==null}pickWith(e){this.pick=e}onPicked(e){let{candidate:t,mode:n}=e.detail;this.ws?.entities[t.entity_id]||(this.extra.set(t.entity_id,{...t,category_confidence:`medium`,category_reason:`From the device search`,device_class:null,unit:null,icon:null,last_changed:null,last_updated:null,capabilities:[],platform:null,registered:!0,hidden:!1,entity_category:null,own:!1}),m.watch(Object.keys(this.ws?.entities??{}).concat([...this.extra.keys()]))),n===`light`?this.assignLight(t.entity_id):n===`reference`?(this.update_(e=>e.reference_occupancy_entity=t.entity_id),this.select(t.entity_id)):this.assignSensor(t.entity_id),this.pick=null}async openReview(){if(!this.ws||!this.draft)return;this.busy=!0,this.reviewError=null,this.reviewOpen=!0;try{this.review=await J(`api/rooms/${encodeURIComponent(this.areaId)}/review`,{room:this.removeRoom?null:jt(this.draft),revision:this.ws.room.revision}),this.confirmed=new Set}catch(e){this.review=null,this.reviewError={message:e instanceof Error?e.message:String(e),code:e instanceof K?e.code:`error`}}finally{this.busy=!1}await this.updateComplete;let e=this.querySelector(`dialog.review-dialog`);e&&!e.open&&e.showModal()}closeReview(){this.querySelector(`dialog.review-dialog`)?.close(),this.reviewOpen=!1}async refreshAndReview(){await this.load(!0,!0),this.changedElsewhere=!1,await this.openReview()}async save(){if(this.ws&&this.draft&&this.review){this.busy=!0,this.reviewError=null;try{let e=await J(`api/rooms/${encodeURIComponent(this.areaId)}/save`,{room:this.removeRoom?null:jt(this.draft),revision:this.ws.room.revision,confirmed:[...this.confirmed]});this.applyWorkspace(e.workspace,!1),this.seenRooms=p.roomsVersion,this.closeReview(),this.review=null,this.changedElsewhere=!1,this.notice=e.result.applied===`live`?`Saved. The integration applied the change at once.`:e.result.applied===`none`?`Nothing changed.`:`Saved. The integration reloads to apply it; no light or cover is switched by the reload.`,this.loadRooms()}catch(e){let t=e instanceof K?e.code:`error`;t===`stale_revision`||t===`confirmation_required`?(await this.refreshAndReview(),this.reviewError={code:t,message:t===`stale_revision`?`The room was changed elsewhere while you were reviewing. The review below compares your edits with the current configuration.`:`The consequences changed since the review. Check them again below.`}):this.reviewError={message:e instanceof Error?e.message:String(e),code:t}}finally{this.busy=!1}}}renderHead(e){let t=Q[e.summary.status];return E`<header class="editor-head">
      <a class="btn back-btn" href="#/rooms" data-testid="back-to-rooms">${N(`arrow-left`,20)}<span>Rooms</span></a>
      <span class="room-icon">${N(Ht(e.area.icon),24)}</span>
      <div class="editor-title">
        <h2 tabindex="-1" data-testid="room-title">${e.area.name}</h2>
        <span class="chips">${I(t.tone,t.label)}
          ${e.summary.enabled?Ut(e.room.occupancy):O}
          ${e.room.lighting?I(e.room.lighting.automatic?`info`:`neutral`,e.room.lighting.automatic?`Lighting: Automatic`:`Lighting: Observing`):O}</span>
      </div>
      <div class="editor-actions">
        <button type="button" class="icon-btn panel-toggle" aria-label=${this.leftOpen?`Hide the room list`:`Show the room list`} aria-pressed=${this.leftOpen?`true`:`false`} @click=${()=>this.leftOpen=!this.leftOpen}>${N(`dock-left`)}</button>
        <button type="button" class="icon-btn panel-toggle" aria-label=${this.inspectorOpen?`Hide the inspector`:`Show the inspector`} aria-pressed=${this.inspectorOpen?`true`:`false`} @click=${()=>this.inspectorOpen=!this.inspectorOpen}>${N(`dock-right`)}</button>
        <button type="button" class="icon-btn" aria-label="Reload from Home Assistant" @click=${()=>void this.load(!0,!0)}>${N(`refresh`)}</button>
      </div>
    </header>`}renderUnsaved(){let e=this.changes(),t=this.ws?.status.writable,n=p.link===`offline`;return E`<div class="unsaved-bar ${e.length?`visible`:``}" role="region" aria-label="Unsaved changes" data-testid="unsaved-bar" ?hidden=${!e.length}>
      <span class="unsaved-text">${N(`pencil`,20)}<span><strong>${H(e.length,`unsaved change`,`unsaved changes`)}</strong>
        <span class="muted"> · saved only after you review them</span></span></span>
      <span class="row-actions">
        <button type="button" class="btn" data-testid="discard" @click=${()=>this.discard()}>${N(`undo`,20)}Discard</button>
        <button type="button" class="btn btn-primary" data-testid="review" ?disabled=${!t||n||this.busy} title=${t?``:`The integration is not running or cannot be written`} @click=${()=>void this.openReview()}>
          ${N(`eye`,20)}Review changes
        </button>
      </span>
    </div>`}renderLeft(e){let t=W(this.roomQuery).split(/\s+/).filter(Boolean),n=(this.rooms??[]).filter(e=>!t.length||t.every(t=>W(e.name).includes(t))),r=Object.values(e.entities).map(e=>this.entity(e.entity_id)).filter(t=>!this.assignment(t.entity_id)&&t.area_id===e.area.id&&(Yt.has(t.category)||t.category===`light`||t.category===`light_group`));return E`<aside class="ws-left" aria-label="Rooms and available devices">
      <section class="panel-section">
        <h3 class="panel-title">${N(`floor-plan`,18)}Rooms</h3>
        <input class="panel-search" type="search" placeholder="Find a room" aria-label="Find a room" .value=${this.roomQuery} @input=${e=>this.roomQuery=e.target.value} />
        <nav class="room-nav" aria-label="Rooms">
          ${n.map(t=>E`<a href="#/rooms/${encodeURIComponent(t.area_id)}/${this.tab}" class=${t.area_id===e.area.id?`active`:``} aria-current=${t.area_id===e.area.id?`page`:O}>
              <span class="dot status-dot-${t.status}" aria-hidden="true"></span><span class="label">${t.name}</span>
              ${t.occupancy?.occupied?E`<span class="occupied-dot" title="Occupied">${N(`account`,16)}</span>`:O}
            </a>`)}
        </nav>
      </section>
      <section class="panel-section">
        <h3 class="panel-title">${N(`plus`,18)}Available in ${e.area.name}</h3>
        ${r.length?E`<ul class="device-palette" data-testid="available-devices">
              ${r.map(t=>E`<li>
                <button type="button" class="palette-item ${this.selected===t.entity_id?`selected`:``}" @click=${()=>this.select(t.entity_id)}>
                  ${N(e.categories[t.category]?.icon??`puzzle`,20)}
                  <span class="palette-text"><span class="palette-name">${t.name}</span><span class="muted small">${t.category_label}</span></span>
                </button>
                <button type="button" class="icon-btn add-btn" aria-label="Assign ${t.name}" data-assign=${t.entity_id}
                  @click=${()=>t.domain===`light`?this.assignLight(t.entity_id):this.assignSensor(t.entity_id)}>${N(`plus`,20)}</button>
              </li>`)}
            </ul>`:E`<p class="muted small">Every sensor and light in this area is assigned. Devices from other areas can be added with
              “Add sensor” or “Add light”.</p>`}
        <div class="palette-actions">
          <button type="button" class="btn" @click=${()=>this.pickWith(`sensor`)}>${N(`motion-sensor`,18)}Add sensor</button>
          <button type="button" class="btn" @click=${()=>this.pickWith(`light`)}>${N(`lightbulb`,18)}Add light</button>
        </div>
      </section>
    </aside>`}renderTabs(){return E`<nav class="tabs" role="tablist" aria-label="Room sections">
      ${qt.map(e=>E`<a role="tab" href="#/rooms/${encodeURIComponent(this.areaId)}/${e.key}" aria-selected=${this.tab===e.key?`true`:`false`} class=${this.tab===e.key?`active`:``} data-tab=${e.key}>
        ${N(e.icon,18)}<span>${e.title}</span>${e.key===`diagnostics`&&this.attentionCount()?E`<span class="badge">${this.attentionCount()}</span>`:O}
      </a>`)}
    </nav>`}attentionCount(){return(this.ws?.summary.warnings??[]).filter(e=>e.level!==`info`).length}laterModule(e,t){return E`<p class="notice notice-info later-note">${N(`info`,20)}<span><strong>${e} stays in the integration for now.</strong>
      This page shows it read-only. Configure it in ${t}; it moves to the App in a later phase, after your approval.</span></p>`}renderOverview(e){let t=this.draft,n=e.summary,r=e.room.occupancy,i=e.room.lighting,a=n.warnings;return E`<div class="tab-grid">
      <section class="card" data-testid="room-status-card">
        <header class="card-head">${N(`floor-plan`)}<h3>Room configuration</h3>${I(Q[n.status].tone,Q[n.status].label)}</header>
        <label class="switch-row" data-testid="room-enabled">
          <input type="checkbox" role="switch" .checked=${t.enabled&&!this.removeRoom} @change=${e=>this.update_(t=>t.enabled=e.target.checked)} />
          <span><strong>Observe occupancy in this room</strong><span class="muted block small">The integration computes occupancy from the assigned sensors and creates its room entities. Turning this off removes them.</span></span>
        </label>
        ${L([[`Sensors`,H(t.sensors.length,`sensor`,`sensors`)],[`Lights`,H(t.lights.length,`light`,`lights`)],[`Reference occupancy`,t.reference_occupancy_entity?this.name(t.reference_occupancy_entity):`None`],[`Adjacent rooms`,t.adjacent_rooms.length?t.adjacent_rooms.map(e=>this.areaName(e)).join(`, `):`None`]])}
        ${e.room.configured?E`<button type="button" class="btn btn-danger" data-testid="remove-room" @click=${()=>(this.removeRoom=!0,this.notice=null)}>${N(`delete`,20)}Remove room configuration…</button>`:E`<p class="muted small">This area has no room configuration yet. Assign sensors and lights, turn on observing, then review and save.</p>`}
        ${this.removeRoom?E`<p class="notice notice-warning">${N(`warning`,20)}<span>The room configuration will be removed when you review and save. Use Discard to keep it.</span></p>`:O}
      </section>
      <section class="card" data-testid="room-occupancy-card">
        <header class="card-head">${N(`account`)}<h3>Occupancy now</h3>${n.enabled?Ut(r):O}</header>
        ${r?L([[`Decision`,r.reason],[`Confidence`,r.confidence],[`Last activity`,r.last_activity?`${V(r.last_activity)}${r.last_activity_source?` · ${this.name(r.last_activity_source)}`:``}`:null],[`Vacant at`,r.vacancy_at?V(r.vacancy_at):null]]):E`<p class="muted">${n.enabled?`The integration has not reported a decision for this room (it may be starting or not running).`:`Occupancy is not observed in this room.`}</p>`}
        <p class="muted small">From the Smart Occupancy integration's own occupancy engine; nothing is estimated here.</p>
      </section>
      <section class="card">
        <header class="card-head">${N(`home`)}<h3>Devices</h3></header>
        <div class="stat-tiles">
          <div class="stat"><span class="stat-value tone-ok">${n.devices.online}</span><span class="stat-label">online</span></div>
          <div class="stat"><span class="stat-value ${n.devices.offline?`tone-error`:``}">${n.devices.offline}</span><span class="stat-label">offline</span></div>
          <div class="stat"><span class="stat-value ${n.devices.unavailable_entities?`tone-warning`:``}">${n.devices.unavailable_entities}</span><span class="stat-label">entities unavailable</span></div>
          <div class="stat"><span class="stat-value">${n.unassigned_candidates}</span><span class="stat-label">found, not assigned</span></div>
        </div>
        <a class="text-link" href="#/rooms/${encodeURIComponent(this.areaId)}/devices">Manage devices ${N(`chevron-right`,18)}</a>
      </section>
      <section class="card" data-testid="room-ownership">
        <header class="card-head">${N(`shield-check`)}<h3>Who runs what</h3></header>
        <ul class="owner-list">
          <li>${N(`robot`,20)}<span><strong>Automation:</strong> Smart Occupancy integration${n.automation.running?``:` (not running)`} — occupancy, lighting, switches and curtains run there, unchanged.</span></li>
          <li>${N(`lightbulb-group`,20)}<span><strong>Lighting:</strong> ${i?i.automatic?`Automatic (the integration controls the lights)`:`Observing (no light commands)`:`—`}</span></li>
          <li>${N(`light-switch`,20)}<span><strong>Switches:</strong> ${n.automation.switches_active?`${n.automation.switches_active} Active`:`none Active`}</span></li>
          <li>${N(`curtains`,20)}<span><strong>Curtains:</strong> ${n.automation.curtains_automatic?`${n.automation.curtains_automatic} window(s) Automatic`:`none Automatic`}</span></li>
          <li>${N(`pencil`,20)}<span><strong>This App:</strong> edits the room's configuration in the integration. It sends no device commands.</span></li>
        </ul>
      </section>
      <section class="card wide" data-testid="room-warnings">
        <header class="card-head">${N(`warning`)}<h3>Needs attention</h3>${a.length?I(a.some(e=>e.level===`error`)?`error`:a.some(e=>e.level===`warning`)?`warning`:`info`,String(a.length)):I(`ok`,`Nothing`)}</header>
        ${a.length?E`<ul class="warning-list">${a.map(e=>E`<li class="level-${e.level}">
              ${N(e.level===`error`?`error`:e.level===`warning`?`warning`:`info`,20)}
              <span><strong>${e.title}</strong>${e.source===`integration`?E` <span class="muted small">(integration)</span>`:O}<span class="block">${e.detail}</span>${e.action?E`<span class="muted small block">${e.action}</span>`:O}</span>
              ${e.entity_id?E`<button type="button" class="btn" @click=${()=>this.select(e.entity_id)}>Inspect</button>`:O}
            </li>`)}</ul>`:E`<p class="muted">No warnings for this room.</p>`}
      </section>
    </div>`}deviceRows(e){let t=new Set(Object.keys(e.entities).concat([...this.extra.keys()])),n=this.draft;for(let e of n.sensors)t.add(e.entity_id);for(let e of n.lights)t.add(e);let r=W(this.deviceQuery).split(/\s+/).filter(Boolean);return[...t].map(e=>this.entity(e)).filter(e=>{let t=this.assignment(e.entity_id);return this.deviceFilter===`assigned`&&!t||this.deviceFilter===`unassigned`&&t||this.deviceFilter===`attention`&&e.availability===`ok`?!1:!r.length||r.every(t=>W(`${e.name} ${e.entity_id} ${e.category_label}`).includes(t))}).sort((e,t)=>e.name.localeCompare(t.name))}assignmentChip(e){let t=this.assignment(e);return t?t.kind===`light`?I(`info`,`Light`):t.kind===`reference`?I(`info`,`Reference`):I(t.sensor.enabled===!1?`pending`:`info`,`${this.kindLabel(t.sensor.kind).split(` (`)[0]} · ${this.roleLabel(t.sensor.role)}${t.sensor.enabled===!1?` · excluded`:``}`):I(`neutral`,`Not assigned`)}quickAction(e){return this.assignment(e.entity_id)?E`<button type="button" class="btn" data-unassign=${e.entity_id} @click=${t=>(t.stopPropagation(),this.unassign(e.entity_id))}>${N(`minus`,18)}Remove</button>`:e.domain===`light`?E`<button type="button" class="btn" data-assign=${e.entity_id} @click=${t=>(t.stopPropagation(),this.assignLight(e.entity_id))}>${N(`plus`,18)}Assign</button>`:Yt.has(e.category)||X(this.ws?.schema??null,e.entity_id,e.suggested_kind).length?E`<button type="button" class="btn" data-assign=${e.entity_id} @click=${t=>(t.stopPropagation(),this.assignSensor(e.entity_id))}>${N(`plus`,18)}Assign</button>`:E`<span class="muted small">Read-only here</span>`}renderDevices(e){let t=this.deviceRows(e);return E`<div class="devices-tab">
      <div class="table-toolbar">
        <label class="search-field">${N(`search`,20)}<input type="search" placeholder="Search devices" aria-label="Search devices" .value=${this.deviceQuery} @input=${e=>this.deviceQuery=e.target.value} /></label>
        <fieldset class="segmented" aria-label="Show">
          <legend class="visually-hidden">Show</legend>
          ${[`all`,`assigned`,`unassigned`,`attention`].map(e=>E`<label class=${this.deviceFilter===e?`selected`:``}><input type="radio" name="device-filter" .checked=${this.deviceFilter===e} @change=${()=>this.deviceFilter=e} />${{all:`All`,assigned:`Assigned`,unassigned:`Not assigned`,attention:`Needs attention`}[e]}</label>`)}
        </fieldset>
        <span class="row-actions">
          <button type="button" class="btn" data-testid="add-sensor" @click=${()=>this.pickWith(`sensor`)}>${N(`plus`,18)}Add sensor</button>
          <button type="button" class="btn" data-testid="add-light" @click=${()=>this.pickWith(`light`)}>${N(`plus`,18)}Add light</button>
        </span>
      </div>
      ${Jt.map(n=>{let r=t.filter(t=>(e.categories[t.category]?.group??`other`)===n.key);return r.length?E`<section class="device-group">
          <h3 class="section-title">${n.title} <span class="muted">${r.length}</span></h3>
          <div class="table-wrap"><table class="device-table">
            <thead><tr><th scope="col">Device</th><th scope="col">Type</th><th scope="col">Status</th><th scope="col">State</th><th scope="col">Assignment</th><th scope="col"><span class="visually-hidden">Action</span></th></tr></thead>
            <tbody>${r.map(t=>E`<tr class=${this.selected===t.entity_id?`selected`:``} data-entity=${t.entity_id} @click=${()=>this.select(t.entity_id)} tabindex="0" @keydown=${e=>e.key===`Enter`&&this.select(t.entity_id)}>
              <td><span class="cell-main">${N(e.categories[t.category]?.icon??`puzzle`,20)}<span><span class="cell-name">${t.name}</span><span class="muted small block mono">${t.entity_id}</span>
                ${t.area_id&&t.area_id!==e.area.id?E`<span class="small tone-warning block">${N(`swap`,14)} From ${t.area??t.area_id}</span>`:O}</span></span></td>
              <td><span class="has-tip" title=${t.category_reason}>${t.category_label}</span>${t.category_confidence===`low`?E`<span class="muted small block">uncertain</span>`:O}</td>
              <td>${$(t.availability)}</td>
              <td><span class="nowrap">${t.state??`—`}${t.unit?` ${t.unit}`:``}</span><span class="muted small block">${V(t.last_changed)}</span></td>
              <td>${this.assignmentChip(t.entity_id)}</td>
              <td class="cell-action">${this.quickAction(t)}</td>
            </tr>`)}</tbody>
          </table></div>
        </section>`:O})}
      ${t.length?O:E`<div class="empty-state">${N(`home`,36)}<p class="muted">No devices match. Devices are found from this area in Home Assistant; use “Add sensor” or “Add light” for devices in other areas.</p></div>`}
    </div>`}renderPresence(e){let t=this.draft,n=e.schema,r=new Map((e.room.occupancy?.signals??[]).map(e=>[e.entity_id,e])),i=e.room.lighting?.zones??[],a=e.timing??{},o=e.room.config.timing??{};return E`<div class="tab-stack">
      <section class="card">
        <header class="card-head">${N(`motion-sensor`)}<h3>Occupancy sensors</h3>
          <button type="button" class="btn" data-testid="presence-add-sensor" @click=${()=>this.pickWith(`sensor`)}>${N(`plus`,18)}Add sensor</button></header>
        ${t.sensors.length?E`<div class="table-wrap"><table class="sensor-table" data-testid="sensor-table">
              <thead><tr><th scope="col">Sensor</th><th scope="col">Type</th><th scope="col">Role</th><th scope="col" class="center">Inverted</th><th scope="col" class="center">Included</th><th scope="col">Now</th><th scope="col"><span class="visually-hidden">Remove</span></th></tr></thead>
              <tbody>${t.sensors.map(e=>{let t=this.entity(e.entity_id),a=r.get(e.entity_id),o=i.filter(t=>t.sensors.includes(e.entity_id)).map(e=>e.name);return E`<tr data-sensor=${e.entity_id} class=${this.selected===e.entity_id?`selected`:``}>
                  <td><button type="button" class="link-btn" @click=${()=>this.select(e.entity_id)}>${t.name}</button><span class="muted small block mono">${e.entity_id}</span>
                    ${o.length?E`<span class="small muted block">Zones: ${o.join(`, `)}</span>`:O}</td>
                  <td><select aria-label="Type of ${t.name}" .value=${e.kind} @change=${t=>this.setSensor(e.entity_id,{kind:t.target.value})}>
                    ${X(n,e.entity_id,e.kind).map(t=>E`<option value=${t.value} ?selected=${t.value===e.kind}>${t.label}</option>`)}
                  </select></td>
                  <td><select aria-label="Role of ${t.name}" title=${Xt[e.role]??``} .value=${e.role} @change=${t=>this.setSensor(e.entity_id,{role:t.target.value})}>
                    ${(n?.roles??[]).map(t=>E`<option value=${t.value} ?selected=${t.value===e.role}>${t.label.split(`:`)[0]}</option>`)}
                  </select></td>
                  <td class="center"><input type="checkbox" class="check" aria-label="${t.name} is inverted (active when off)" .checked=${!!e.inverted} @change=${t=>this.setSensor(e.entity_id,{inverted:t.target.checked})} /></td>
                  <td class="center"><input type="checkbox" class="check" aria-label="Include ${t.name} in occupancy" .checked=${e.enabled!==!1} @change=${t=>this.setSensor(e.entity_id,{enabled:t.target.checked})} /></td>
                  <td>${$(t.availability)}<span class="small block">${a?Zt[a.condition]??a.condition:t.state??`—`}</span></td>
                  <td class="cell-action"><button type="button" class="icon-btn" aria-label="Remove ${t.name}" data-unassign=${e.entity_id} @click=${()=>this.unassign(e.entity_id)}>${N(`delete`,20)}</button></td>
                </tr>`})}</tbody>
            </table></div>
            <p class="muted small">Role: ${Object.entries(Xt).map(([e,t])=>E`<span class="block"><strong>${this.roleLabel(e)}</strong> — ${t}</span>`)}</p>`:E`<div class="empty-state">${N(`motion-sensor`,36)}<p class="muted">No sensors yet. Add a PIR or radar sensor: without one the room's occupancy cannot be observed.</p></div>`}
      </section>
      <section class="card">
        <header class="card-head">${N(`link`)}<h3>Reference and adjacent rooms</h3></header>
        <div class="field-row">
          <span class="field-label">Reference occupancy entity <span class="muted">(optional; your existing helper, for comparison only)</span></span>
          <div class="ref-row">
            <span>${t.reference_occupancy_entity?E`${this.name(t.reference_occupancy_entity)} <span class="muted mono small">${t.reference_occupancy_entity}</span>`:E`<span class="muted">None</span>`}</span>
            <button type="button" class="btn" data-testid="pick-reference" @click=${()=>this.pickWith(`reference`)}>${N(`pencil`,18)}Choose…</button>
            ${t.reference_occupancy_entity?E`<button type="button" class="btn" @click=${()=>this.update_(e=>e.reference_occupancy_entity=null)}>${N(`close`,18)}Clear</button>`:O}
          </div>
        </div>
        <fieldset class="chip-choices">
          <legend class="field-label">Adjacent rooms <span class="muted">(movement into them confirms that someone left through a door)</span></legend>
          ${e.areas.filter(t=>t.id!==e.area.id).map(n=>E`<label class=${t.adjacent_rooms.includes(n.id)?`selected`:``}>
            <input type="checkbox" .checked=${t.adjacent_rooms.includes(n.id)} @change=${e=>this.update_(t=>{t.adjacent_rooms=e.target.checked?[...t.adjacent_rooms,n.id]:t.adjacent_rooms.filter(e=>e!==n.id)})} />${n.name}${e.other_rooms[n.id]?.enabled?O:E`<span class="muted small"> · not observed</span>`}</label>`)}
        </fieldset>
      </section>
      <section class="card">
        <header class="card-head">${N(`history`)}<h3>Presence timing</h3>${I(`neutral`,`Read-only`)}</header>
        ${this.laterModule(`Presence timing`,`the Smart Room Lighting card (Presence tab) or Configure → Occupancy timing`)}
        <div class="table-wrap"><table class="plain-table">
          <thead><tr><th scope="col">Setting</th><th scope="col">Global</th><th scope="col">This room</th></tr></thead>
          <tbody>${Object.entries(a).map(([e,t])=>E`<tr><td>${e.replace(/_/g,` `)}</td><td>${t} s</td><td>${e in o?E`<strong>${o[e]} s</strong>`:E`<span class="muted">global</span>`}</td></tr>`)}</tbody>
        </table></div>
      </section>
    </div>`}renderLighting(e){let t=this.draft,n=e.room.lighting,r=(n?.managed??[]).filter(e=>!t.lights.includes(e));return E`<div class="tab-stack">
      <section class="card">
        <header class="card-head">${N(`lightbulb`)}<h3>Assigned lights</h3>
          <button type="button" class="btn" data-testid="lighting-add-light" @click=${()=>this.pickWith(`light`)}>${N(`plus`,18)}Add light</button></header>
        ${t.lights.length?E`<ul class="entity-rows" data-testid="light-list">${t.lights.map(e=>{let t=this.entity(e);return E`<li data-light=${e} class=${this.selected===e?`selected`:``}>
                <button type="button" class="row-main" @click=${()=>this.select(e)}>${N(t.category===`light_group`?`lightbulb-group`:`lightbulb`,22)}
                  <span><span class="cell-name">${t.name}</span><span class="muted small block mono">${e}</span><span class="small muted block">${t.capabilities.join(` · `)}</span></span></button>
                <span>${$(t.availability)}<span class="small block">${t.state??`—`}</span></span>
                <button type="button" class="icon-btn" aria-label="Remove ${t.name}" data-unassign=${e} @click=${()=>this.unassign(e)}>${N(`delete`,20)}</button>
              </li>`})}</ul>`:E`<div class="empty-state">${N(`lightbulb`,36)}<p class="muted">No lights assigned. Lights with a light-type label in this area can also be managed automatically (see below).</p></div>`}
      </section>
      <section class="card">
        <header class="card-head">${N(`robot`)}<h3>Automatic lighting</h3>${n?I(n.automatic?`info`:`neutral`,n.automatic?`Automatic`:`Observing`):I(`neutral`,`Unknown`)}</header>
        ${this.laterModule(`Lighting behaviour`,`the Smart Room Lighting card`)}
        ${n?L([[`Mode`,n.automatic?`Automatic since ${V(n.activated_at)}`:`Observing — no light commands`],[`Also managed by label`,r.length?r.map(e=>this.name(e)).join(`, `):`None`],[`Relays (lighting outputs)`,n.relays.length?n.relays.map(e=>this.name(e)).join(`, `):`None`],[`Zones`,n.zones.length?n.zones.map(e=>`${e.name}${e.enabled?``:` (off)`}`).join(`, `):`None`],[`When vacant`,n.vacancy_action?.replace(/_/g,` `)??null]]):E`<p class="muted">The integration is not running, so its lighting state cannot be read.</p>`}
      </section>
    </div>`}renderSwitches(e){let t=e.room.lighting,n=new Set((t?.switches??[]).flatMap(e=>[e.event_entity,e.device_id]).filter(Boolean)),r=Object.values(e.entities).filter(e=>e.category===`wall_switch`&&!n.has(e.entity_id)&&!n.has(e.device_id??``));return E`<div class="tab-stack">
      ${this.laterModule(`Wall switches and Priority Inputs`,`the Smart Room Lighting card (Switches)`)}
      <section class="card">
        <header class="card-head">${N(`light-switch`)}<h3>Configured switches</h3></header>
        ${t?.switches.length?E`<ul class="entity-rows">${t.switches.map(t=>E`<li><span class="row-main static">${N(`light-switch`,22)}<span><span class="cell-name">${t.name}</span>
              <span class="muted small block">${t.device_id?e.devices[t.device_id]?.name??`Device`:t.event_entity??``}</span></span></span>
              ${I(t.mode===`control`?`info`:`neutral`,t.mode===`control`?`Active`:`Observing`)}</li>`)}</ul>`:E`<p class="muted">No wall switch or remote is configured for this room.</p>`}
      </section>
      <section class="card">
        <header class="card-head">${N(`remote`)}<h3>Found in this area, not configured</h3></header>
        ${r.length?E`<ul class="entity-rows">${r.map(e=>E`<li><button type="button" class="row-main" @click=${()=>this.select(e.entity_id)}>${N(`remote`,22)}<span><span class="cell-name">${e.name}</span><span class="muted small block mono">${e.entity_id}</span></span></button>${$(e.availability)}</li>`)}</ul>`:E`<p class="muted">No other switch or remote found.</p>`}
      </section>
      ${t?.priority_inputs.length?E`<section class="card"><header class="card-head">${N(`alert-octagon`)}<h3>Priority Inputs</h3></header>
            <ul class="entity-rows">${t.priority_inputs.map(e=>E`<li><span class="row-main static">${N(`alert-octagon`,22)}<span><span class="cell-name">${e.name}</span><span class="muted small block mono">${e.entity_id??`—`}</span></span></span>${I(e.enabled?`info`:`neutral`,e.enabled?`Enabled`:`Off`)}</li>`)}</ul></section>`:O}
    </div>`}renderCurtains(e){let t=e.room.curtains,n=new Set(t.flatMap(e=>e.covers)),r=Object.values(e.entities).filter(e=>e.category===`cover`&&!n.has(e.entity_id));return E`<div class="tab-stack">
      ${this.laterModule(`Curtains`,`the Smart Curtains card`)}
      <section class="card">
        <header class="card-head">${N(`curtains`)}<h3>Windows</h3></header>
        ${t.length?E`<ul class="entity-rows">${t.map(e=>E`<li><span class="row-main static">${N(`window`,22)}<span><span class="cell-name">${e.name}</span>
              <span class="small block">${e.covers.map(e=>E`<span class="cover-state">${this.name(e)}: ${this.entity(e).state??`—`}</span>`)}</span></span></span>
              ${I(e.mode===`automatic`?`info`:`neutral`,e.mode.charAt(0).toUpperCase()+e.mode.slice(1))}</li>`)}</ul>`:E`<p class="muted">No window is configured for this area.</p>`}
      </section>
      <section class="card">
        <header class="card-head">${N(`blinds`)}<h3>Covers found, not in a window</h3></header>
        ${r.length?E`<ul class="entity-rows">${r.map(e=>E`<li><button type="button" class="row-main" @click=${()=>this.select(e.entity_id)}>${N(`curtains`,22)}<span><span class="cell-name">${e.name}</span><span class="muted small block">${e.capabilities.join(` · `)}</span></span></button>${$(e.availability)}</li>`)}</ul>`:E`<p class="muted">None.</p>`}
      </section>
    </div>`}renderDiagnostics(e){let t=this.draft,n=new Set([...t.sensors.map(e=>e.entity_id),...t.lights,...t.reference_occupancy_entity?[t.reference_occupancy_entity]:[]]),r=[...new Set([...Object.keys(e.entities),...n])].map(e=>this.entity(e)),i=e.summary.warnings.filter(e=>[`light_conflict`,`sensor_shared`,`outside_area`].includes(e.code)),a=r.filter(t=>!n.has(t.entity_id)&&t.area_id===e.area.id&&Yt.has(t.category)&&!t.sensor_rooms.length),o=e.room.occupancy,s=e=>{if(!n.has(e.entity_id))return{tone:`neutral`,label:`Present, not assigned`,help:`The device is in this area but the room does not use it.`};let t=Z[e.availability];return{tone:t.tone,label:t.label,help:t.help}};return E`<div class="tab-stack">
      <section class="card">
        <header class="card-head">${N(`stethoscope`)}<h3>Entity status</h3></header>
        <ul class="legend">
          <li>${I(`error`,`Device offline`)} the physical device is unreachable (all its entities unavailable)</li>
          <li>${I(`warning`,`Entity unavailable`)} only this entity is unavailable</li>
          <li>${I(`error`,`Missing`)} the entity does not exist in Home Assistant</li>
          <li>${I(`neutral`,`Present, not assigned`)} found in this area, not used by the room</li>
          <li>${I(`warning`,`Conflict`)} used by several rooms, or from another area</li>
        </ul>
        <div class="table-wrap"><table class="device-table" data-testid="diag-table">
          <thead><tr><th scope="col">Entity</th><th scope="col">Status</th><th scope="col">State</th><th scope="col">Last changed</th><th scope="col">Device</th></tr></thead>
          <tbody>${r.sort((e,t)=>Number(n.has(t.entity_id))-Number(n.has(e.entity_id))||e.name.localeCompare(t.name)).map(t=>{let n=s(t),r=t.device_id?e.devices[t.device_id]:null;return E`<tr data-entity=${t.entity_id} @click=${()=>this.select(t.entity_id)}>
              <td><span class="cell-name">${t.name}</span><span class="muted small block mono">${t.entity_id}</span></td>
              <td><span class="has-tip" title=${n.help}>${I(n.tone,n.label)}</span></td>
              <td>${t.state??`—`}${t.unit?` ${t.unit}`:``}</td>
              <td>${V(t.last_changed)}</td>
              <td>${r?E`${r.name}<span class="muted small block">${r.status}</span>`:E`<span class="muted">—</span>`}</td>
            </tr>`})}</tbody>
        </table></div>
      </section>
      <section class="card">
        <header class="card-head">${N(`swap`)}<h3>Configuration conflicts</h3>${I(i.length?`warning`:`ok`,String(i.length))}</header>
        ${i.length?E`<ul class="warning-list">${i.map(e=>E`<li class="level-${e.level}">${N(`warning`,20)}<span><strong>${e.title}</strong><span class="block">${e.detail}</span></span></li>`)}</ul>`:E`<p class="muted">None.</p>`}
        ${a.length?E`<p class="small">Present but unassigned sensors: ${a.map(e=>e.name).join(`, `)}</p>`:O}
      </section>
      <section class="card">
        <header class="card-head">${N(`puzzle`)}<h3>Integration checks</h3></header>
        ${e.room.problems.length?E`<ul class="warning-list">${e.room.problems.map(e=>E`<li class="level-${e.severity===`error`?`error`:e.severity===`warning`?`warning`:`info`}">${N(e.severity===`error`?`error`:e.severity===`warning`?`warning`:`info`,20)}<span><strong>${e.title}</strong><span class="block">${e.explanation}</span><span class="muted small block">${e.action}</span></span></li>`)}</ul>`:E`<p class="muted">The integration reports no problem for this room.</p>`}
        ${o?.signals?.length?E`<h4 class="section-title">Occupancy decision inputs</h4><div class="table-wrap"><table class="plain-table"><thead><tr><th scope="col">Signal</th><th scope="col">Value</th><th scope="col">Contribution</th><th scope="col">Health</th></tr></thead>
              <tbody>${o.signals.map(e=>E`<tr><td>${this.name(e.entity_id)}</td><td>${e.label}</td><td>${Zt[e.condition]??e.condition}${e.status?E`<span class="muted small block">${e.status}</span>`:O}</td><td>${e.health}</td></tr>`)}</tbody></table></div>`:O}
      </section>
      <section class="card">
        <header class="card-head">${N(`json`)}<h3>Stored configuration</h3>
          <button type="button" class="btn" @click=${()=>At({area_id:e.area.id,revision:e.room.revision,config:e.room.config},`room-${e.area.id}.json`)}>${N(`download`,18)}Download</button></header>
        ${L([[`Revision`,E`<code>${e.room.revision}</code>`],[`Read`,V(e.generated_at)],[`Integration`,`${e.status.integration_version??`—`} (${e.status.integration_state??`unknown`})`]])}
        <textarea class="json-view" readonly aria-label="Stored room configuration">${JSON.stringify(e.room.config,null,2)}</textarea>
      </section>
    </div>`}renderInspector(e){let t=this.selected;if(!t)return E`<aside class="ws-right inspector" aria-label="Inspector">
        <header class="inspector-head"><h3>Inspector</h3><button type="button" class="icon-btn" aria-label="Close the inspector" @click=${()=>this.inspectorOpen=!1}>${N(`close`)}</button></header>
        <div class="empty-state small">${N(`tune`,36)}<p class="muted">Select a device to see its live state, identity and assignment.</p></div>
      </aside>`;let n=this.entity(t),r=this.assignment(t),i=n.device_id?e.devices[n.device_id]:null,a=e.schema,o=X(a,t,n.suggested_kind),s=[...new Set([...n.sensor_rooms,...n.light_rooms])].filter(t=>t!==e.area.id),c=(e.room.lighting?.zones??[]).filter(e=>e.sensors.includes(t)),l=(a?.reference_domains??[`input_boolean`,`binary_sensor`]).includes(n.domain);return E`<aside class="ws-right inspector" aria-label="Inspector" data-testid="inspector">
      <header class="inspector-head">
        <span class="inspector-icon">${N(e.categories[n.category]?.icon??`puzzle`,24)}</span>
        <div><h3>${n.name}</h3><code class="small">${t}</code></div>
        <button type="button" class="icon-btn" aria-label="Close the inspector" @click=${()=>this.select(null)}>${N(`close`)}</button>
      </header>
      <section class="inspector-section">
        <div class="live-state">${$(n.availability)}<span class="state-value">${n.state??`—`}${n.unit?` ${n.unit}`:``}</span></div>
        <p class="muted small">Last changed ${V(n.last_changed)} · updated ${V(n.last_updated)}</p>
        <p class="small">${Z[n.availability]?.help}</p>
      </section>
      <section class="inspector-section" data-testid="inspector-assignment">
        <h4>Assignment</h4>
        ${r?.kind===`sensor`?E`<label class="field"><span class="field-label">Type</span>
                <select data-testid="inspector-kind" .value=${r.sensor.kind} @change=${e=>this.setSensor(t,{kind:e.target.value})}>
                  ${o.map(e=>E`<option value=${e.value} ?selected=${e.value===r.sensor.kind}>${e.label}</option>`)}
                </select></label>
              <label class="field"><span class="field-label">Role</span>
                <select data-testid="inspector-role" .value=${r.sensor.role} @change=${e=>this.setSensor(t,{role:e.target.value})}>
                  ${(a?.roles??[]).map(e=>E`<option value=${e.value} ?selected=${e.value===r.sensor.role}>${e.label.split(`:`)[0]}</option>`)}
                </select><span class="muted small">${Xt[r.sensor.role]}</span></label>
              <label class="switch-row"><input type="checkbox" role="switch" .checked=${!!r.sensor.inverted} @change=${e=>this.setSensor(t,{inverted:e.target.checked})} />
                <span>Inverted <span class="muted small block">Counts as active when it is off (e.g. a “PC idle” sensor).</span></span></label>
              <label class="switch-row"><input type="checkbox" role="switch" .checked=${r.sensor.enabled!==!1} @change=${e=>this.setSensor(t,{enabled:e.target.checked})} />
                <span>Included in occupancy <span class="muted small block">Off keeps the assignment but ignores the sensor.</span></span></label>
              <button type="button" class="btn btn-danger" data-testid="inspector-remove" @click=${()=>this.unassign(t)}>${N(`minus`,18)}Remove from this room</button>`:r?.kind===`light`?E`<p>${I(`info`,`Light of this room`)}</p><button type="button" class="btn btn-danger" data-testid="inspector-remove" @click=${()=>this.unassign(t)}>${N(`minus`,18)}Remove from this room</button>`:r?.kind===`reference`?E`<p>${I(`info`,`Reference occupancy entity`)}</p><button type="button" class="btn" @click=${()=>this.update_(e=>e.reference_occupancy_entity=null)}>${N(`close`,18)}Clear reference</button>`:E`<p class="muted">Not assigned to this room.</p>
                  <div class="assign-buttons">
                    ${n.domain===`light`?E`<button type="button" class="btn btn-primary" data-testid="inspector-assign-light" @click=${()=>this.assignLight(t)}>${N(`plus`,18)}Assign as light</button>`:O}
                    ${o.length&&n.domain!==`light`?o.slice(0,3).map((e,n)=>E`<button type="button" class="btn ${n===0?`btn-primary`:``}" data-testid="inspector-assign-${e.value}" @click=${()=>this.assignSensor(t,e.value)}>${N(`plus`,18)}As ${e.label.split(` (`)[0]}</button>`):O}
                    ${l?E`<button type="button" class="btn" @click=${()=>this.update_(e=>e.reference_occupancy_entity=t)}>${N(`link`,18)}Use as reference</button>`:O}
                  </div>`}
        ${s.length?E`<p class="notice notice-warning small">${N(`swap`,18)}<span>Also used in ${s.map(e=>this.areaName(e)).join(`, `)}.</span></p>`:O}
        ${n.area_id&&n.area_id!==e.area.id?E`<p class="notice notice-warning small">${N(`swap`,18)}<span>This device is in ${n.area??n.area_id} in Home Assistant. It can still be used here; its Home Assistant area is not changed.</span></p>`:O}
        ${c.length?E`<p class="small muted">Used by lighting zones: ${c.map(e=>e.name).join(`, `)}</p>`:O}
      </section>
      <section class="inspector-section">
        <h4>Classification</h4>
        <p><strong>${n.category_label}</strong> <span class="muted small">· ${n.category_confidence===`user`?`your choice`:`${n.category_confidence} confidence`}</span></p>
        <p class="muted small">${n.category_reason}. ${n.category_confidence===`user`?``:`Correct it by choosing the type when you assign it.`}</p>
        ${n.capabilities.length?E`<p class="cap-list">${n.capabilities.map(e=>E`<span class="cap">${e}</span>`)}</p>`:O}
      </section>
      <section class="inspector-section">
        <h4>Device</h4>
        ${L(i?[[`Name`,i.name],[`Status`,i.status===`online`?`Online`:i.status===`offline`?`Offline (all entities unavailable)`:i.status===`partial`?`Partly unavailable`:i.status],[`Manufacturer`,i.manufacturer],[`Model`,[i.model,i.model_id].filter(Boolean).join(` · `)||null],[`Firmware`,i.sw_version],[`Identity`,i.identity.length?E`${i.identity.map(e=>E`<code class="block small">${e}</code>`)}`:null],[`Area`,i.area],[`Entities`,String(i.entity_ids.length)]]:[[`Device`,`None (entity without a device)`],[`Area`,n.area],[`Integration`,n.platform]])}
      </section>
    </aside>`}renderReview(){if(!this.reviewOpen)return O;let e=this.review,t=this.reviewError,n=[{key:`added`,title:`Added`},{key:`removed`,title:`Removed`},{key:`classification`,title:`Changed classifications`},{key:`settings`,title:`Enabled / disabled settings`},{key:`relations`,title:`Reference and adjacent rooms`}],r=t=>t?e?.entities[t]?.name??this.name(t):`—`,i=e?.requires_confirmation??[],a=!!(e&&!e.errors.length&&!e.stale&&e.changes.length&&i.every(e=>this.confirmed.has(e))&&e.writable&&p.link!==`offline`);return E`<dialog class="sheet sheet-full review-dialog" aria-labelledby="review-title" data-testid="review-dialog"
        @close=${()=>this.reviewOpen=!1}>
      <div class="sheet-inner">
        <header class="sheet-head">
          <h2 id="review-title">Review changes · ${this.ws?.area.name}</h2>
          <button type="button" class="icon-btn" aria-label="Close" @click=${()=>this.closeReview()}>${N(`close`)}</button>
        </header>
        <div class="sheet-body">
          ${t?E`<p class="notice notice-error" role="alert">${N(`error`,20)}<span>${t.message}
              ${[`connection_lost`,`network`,`not_connected`,`api_unavailable`,`not_loaded`].includes(t.code)?E`<button type="button" class="btn" data-testid="retry-review" @click=${()=>void this.refreshAndReview()}>${N(`refresh`,18)}Reload and review again</button>`:O}</span></p>`:O}
          ${this.busy&&!e?E`<p class="muted" role="status"><span class="spinner" aria-hidden="true"></span> Checking with the integration…</p>`:O}
          ${e?.stale?E`<p class="notice notice-warning" data-testid="stale-notice">${N(`warning`,20)}<span>This room was changed elsewhere (for example in the integration's own room editor) after you opened it.
              <button type="button" class="btn" data-testid="refresh-review" @click=${()=>void this.refreshAndReview()}>${N(`refresh`,18)}Load the current configuration and review again</button></span></p>`:O}
          ${e?E`
                ${e.changes.length?O:E`<p class="muted">Nothing changes compared with the stored configuration.</p>`}
                ${n.map(t=>{let n=e.changes.filter(e=>Lt(e.type)===t.key);return n.length?E`<section class="review-block" data-group=${t.key}><h3>${t.title}</h3><ul class="change-list">
                        ${n.map(e=>E`<li class="change-${t.key}">${N(t.key===`added`?`plus`:t.key===`removed`?`minus`:`pencil`,18)}<span>${It(e,r,e=>this.areaName(e),this.kindLabel,this.roleLabel)}</span></li>`)}
                      </ul></section>`:O})}
                ${e.errors.length?E`<section class="review-block" data-group="errors"><h3 class="tone-error">Cannot be saved</h3><ul class="change-list">${e.errors.map(e=>E`<li>${N(`error`,18)}<span>${e.message}</span></li>`)}</ul></section>`:O}
                ${e.conflicts.length||e.problems_new.length?E`<section class="review-block" data-group="conflicts"><h3>Potential conflicts</h3><ul class="change-list">
                      ${e.conflicts.map(e=>E`<li>${N(`swap`,18)}<span>${r(e.entity_id)}: ${e.code===`outside_area`?`is in ${e.rooms[0]} in Home Assistant (allowed; its area is not changed)`:e.code===`light_conflict`?`is already a light of ${e.rooms.join(`, `)}`:e.code===`category_mismatch`?`is a ${e.rooms[0]}, assigned as a sensor`:`is also a sensor of ${e.rooms.join(`, `)}`}</span></li>`)}
                      ${e.problems_new.map(e=>E`<li>${N(e.severity===`error`?`error`:`warning`,18)}<span><strong>${e.title}</strong> — ${e.explanation}</span></li>`)}
                    </ul></section>`:O}
                ${e.attention.length?E`<section class="review-block" data-group="attention"><h3>Missing or unavailable</h3><ul class="change-list">${e.attention.map(e=>E`<li>${$(e.availability)}<span>${e.name} <span class="muted mono small">${e.entity_id}</span></span></li>`)}</ul></section>`:O}
                ${e.problems_resolved.length?E`<section class="review-block"><h3>Resolved</h3><ul class="change-list">${e.problems_resolved.map(e=>E`<li>${N(`ok`,18)}<span>${e.title}${e.name?` · ${e.name}`:``}</span></li>`)}</ul></section>`:O}
                ${e.impacts.length?E`<section class="review-block" data-group="impacts"><h3>What saving does</h3>
                      ${e.impacts.map(e=>e.level===`confirm`?E`<label class="confirm-row" data-impact=${e.code}><input type="checkbox" .checked=${this.confirmed.has(e.code)} @change=${t=>{let n=new Set(this.confirmed);t.target.checked?n.add(e.code):n.delete(e.code),this.confirmed=n}} /><span><strong>${e.title}</strong><span class="muted block">${e.detail}</span></span></label>`:E`<p class="impact-info">${N(`info`,18)}<span><strong>${e.title}.</strong> ${e.detail}</span></p>`)}
                    </section>`:O}
                <p class="muted small">No device is switched by saving. The configuration is stored in the Smart Occupancy integration, which validates it again before saving.</p>
              `:O}
        </div>
        <footer class="sheet-foot">
          <button type="button" class="btn" @click=${()=>this.closeReview()}>Cancel</button>
          <button type="button" class="btn btn-primary" data-testid="save" ?disabled=${!a||this.busy} @click=${()=>void this.save()}>
            ${N(`save`,20)}${this.busy&&e?`Saving…`:i.length?`Confirm and save`:`Save`}
          </button>
        </footer>
      </div>
    </dialog>`}render(){let e=this.ws;if(!e)return E`${this.loadError?E`<div class="card empty-state" role="alert">${N(`error`,40)}<h3>The room could not be loaded</h3><p class="muted">${this.loadError}</p>
            <div class="actions"><a class="btn" href="#/rooms">${N(`arrow-left`,20)}All rooms</a><button type="button" class="btn" @click=${()=>void this.load(!1)}>${N(`refresh`,20)}Try again</button></div></div>`:E`<div class="card loading" role="status"><span class="spinner" aria-hidden="true"></span><span>Loading the room…</span></div>`}`;let t;switch(this.tab){case`devices`:t=this.renderDevices(e);break;case`presence`:t=this.renderPresence(e);break;case`lighting`:t=this.renderLighting(e);break;case`switches`:t=this.renderSwitches(e);break;case`curtains`:t=this.renderCurtains(e);break;case`diagnostics`:t=this.renderDiagnostics(e);break;default:t=this.renderOverview(e)}return E`<div class="room-editor" data-area=${e.area.id}>
      ${this.renderHead(e)}
      ${Wt(e.status)}
      ${this.changedElsewhere?E`<p class="notice notice-warning" data-testid="changed-elsewhere">${N(`warning`,20)}<span>This room's configuration was changed elsewhere while you were editing. Your unsaved changes are kept;
          <button type="button" class="btn" @click=${()=>void this.refreshAndReview()}>${N(`refresh`,18)}Review them against the current configuration</button></span></p>`:O}
      ${p.link===`offline`?E`<p class="notice notice-error">${N(`offline`,20)}<span>The App backend cannot be reached. Your edits are kept here; saving is possible again when the connection is back.</span></p>`:O}
      ${this.notice?E`<p class="notice notice-info" role="status" data-testid="saved-notice">${N(`ok`,20)}<span>${this.notice}</span></p>`:O}
      ${this.renderUnsaved()}
      <div class="workspace ${this.leftOpen?``:`left-collapsed`} ${this.inspectorOpen?`inspector-open`:``}">
        ${this.leftOpen?this.renderLeft(e):O}
        <section class="ws-center" aria-label="Room">${this.renderTabs()}<div class="tab-panel" role="tabpanel">${t}</div></section>
        ${this.inspectorOpen?this.renderInspector(e):O}
        ${this.inspectorOpen?E`<div class="inspector-scrim" @click=${()=>this.inspectorOpen=!1}></div>`:O}
      </div>
      ${this.pick?E`<sl-entity-pick .mode=${this.pick} .areaId=${e.area.id} .schema=${e.schema} .exclude=${this.pick===`light`?this.draft.lights:this.draft.sensors.map(e=>e.entity_id)}
          @picked=${e=>this.onPicked(e)} @close=${()=>this.pick=null}></sl-entity-pick>`:O}
      ${this.renderReview()}
    </div>`}};customElements.define(`sl-room-editor`,Qt);var $t=null,en=class extends F{static{this.properties={mode:{type:String},areaId:{type:String},schema:{attribute:!1},exclude:{attribute:!1},all:{state:!0},query:{state:!0},scope:{state:!0},error:{state:!0}}}constructor(){super(),this.mode=`sensor`,this.areaId=``,this.schema=null,this.exclude=[],this.all=null,this.query=``,this.scope=`suggested`,this.error=null}async connectedCallback(){super.connectedCallback(),await this.updateComplete,this.querySelector(`dialog`)?.showModal(),this.querySelector(`input[type=search]`)?.focus();try{(!$t||Date.now()-$t.at>3e4)&&($t={at:Date.now(),data:(await q(`api/rooms/candidates`)).entities}),this.all=$t.data}catch(e){this.error=String(e)}}allowed(e){return this.exclude.includes(e.entity_id)?!1:this.mode===`light`?e.domain===`light`:this.mode===`reference`?(this.schema?.reference_domains??[`input_boolean`,`binary_sensor`]).includes(e.domain):X(this.schema,e.entity_id,e.suggested_kind).length>0&&e.domain!==`light`}suggested(e){return this.mode===`light`?!0:this.mode===`reference`?e.domain===`input_boolean`||e.category===`presence_input`:Yt.has(e.category)&&e.category!==`temperature`&&e.category!==`illuminance`}close(){this.querySelector(`dialog`)?.close()}choose(e){this.dispatchEvent(new CustomEvent(`picked`,{detail:{candidate:e,mode:this.mode}}))}render(){let e=W(this.query).split(/\s+/).filter(Boolean),t=(this.all??[]).filter(e=>this.allowed(e)&&(this.scope===`all`||this.suggested(e))).filter(t=>!e.length||e.every(e=>W(`${t.name} ${t.entity_id} ${t.area??``} ${t.category_label}`).includes(e))).sort((e,t)=>Number(t.area_id===this.areaId)-Number(e.area_id===this.areaId)||e.name.localeCompare(t.name)),n=t.slice(0,80),r={sensor:`Add a sensor`,light:`Add a light`,reference:`Choose the reference occupancy entity`}[this.mode];return E`<dialog class="sheet sheet-full pick-dialog" aria-labelledby="pick-title" data-testid="entity-pick"
        @close=${()=>this.dispatchEvent(new CustomEvent(`close`))}
        @click=${e=>e.target.matches(`dialog`)&&this.close()}>
      <div class="sheet-inner">
        <header class="sheet-head"><h2 id="pick-title">${r}</h2><button type="button" class="icon-btn" aria-label="Close" @click=${()=>this.close()}>${N(`close`)}</button></header>
        <div class="picker-search">${N(`search`,20)}<input type="search" autocomplete="off" spellcheck="false" placeholder="Search name, entity ID or area" aria-label="Search" .value=${this.query} @input=${e=>this.query=e.target.value} /></div>
        ${this.mode===`light`?O:E`<fieldset class="segmented pick-scope"><legend class="visually-hidden">Show</legend>
              <label class=${this.scope===`suggested`?`selected`:``}><input type="radio" name="scope" .checked=${this.scope===`suggested`} @change=${()=>this.scope=`suggested`} />Likely ${this.mode===`reference`?`helpers`:`presence sensors`}</label>
              <label class=${this.scope===`all`?`selected`:``}><input type="radio" name="scope" .checked=${this.scope===`all`} @change=${()=>this.scope=`all`} />All allowed</label></fieldset>`}
        <div class="picker-results">
          ${this.error?E`<p class="notice notice-error">${this.error}</p>`:this.all?n.length?E`<ul role="listbox" aria-label=${r}>
            ${n.map(e=>E`<li role="option" aria-selected="false" data-value=${e.entity_id} @click=${()=>this.choose(e)}>
              <span class="opt-primary">${e.name}</span>
              <span class="opt-secondary">${e.entity_id} · ${e.category_label} · ${e.area??`no area`}
                ${e.area_id&&e.area_id!==this.areaId?E`<span class="tone-warning"> · other area</span>`:O}
                ${e.sensor_rooms.length||e.light_rooms.length?E`<span class="tone-warning"> · already used in another room</span>`:O}
                ${e.availability===`ok`?O:E`<span class="tone-warning"> · ${Z[e.availability]?.label}</span>`}</span>
            </li>`)}
          </ul>${t.length>n.length?E`<p class="muted small pad">Showing ${n.length} of ${t.length}. Type more to narrow the list.</p>`:O}`:E`<p class="muted pad">Nothing matches.</p>`:E`<p class="muted pad">Loading…</p>`}
        </div>
      </div>
    </dialog>`}};customElements.define(`sl-entity-pick`,en);var tn=[{value:`auto`,label:`Automatic`},{value:`light`,label:`Light`},{value:`dark`,label:`Dark`}],nn=class extends F{static{this.properties={theme:{state:!0},advanced:{state:!0},settings:{state:!0}}}constructor(){super(),this.theme=o(),this.advanced=l(),this.settings=null}connectedCallback(){super.connectedCallback(),this.advanced&&this.loadSettings()}async loadSettings(){try{this.settings=(await f(`api/diagnostics`)).settings}catch{this.settings=null}}chooseTheme(e){this.theme=e,s(e)}toggleAdvanced(e){this.advanced=e.target.checked,u(this.advanced),this.advanced&&!this.settings&&this.loadSettings()}renderAdvanced(){let e=p.overview,t=this.settings,n=new URL(document.baseURI).pathname;return E`<section class="card wide" data-testid="advanced-settings">
      <header class="card-head">${N(`cog`)}<h3>Advanced</h3></header>
      <p class="muted">
        Technical details, for troubleshooting. App options (such as the log level) are changed in Home Assistant:
        Settings → Apps → Smart Occupancy &amp; Lighting → Configuration.
      </p>
      ${L([[`Log level`,t?.log_level],[`Home Assistant API`,t?.home_assistant_websocket],[`Supervisor API`,t?.supervisor_api],[`API credentials`,t?.token],[`Accepted clients`,Array.isArray(t?.allowed_clients)?(t?.allowed_clients).join(`, `):null],[`Interface path`,n],[`Live updates`,z(p.link).label],[`Development mode`,e?.app.dev_mode?`On`:`Off`]])}
    </section>`}render(){let e=p.overview;return E`
      ${R(`Settings`,`Appearance and information about the App.`)}
      <div class="grid grid-2">
        <section class="card" data-testid="appearance">
          <header class="card-head">${N(`palette`)}<h3>Appearance</h3></header>
          <fieldset class="segmented" aria-label="Theme">
            <legend>Theme</legend>
            ${tn.map(e=>E`<label class=${this.theme===e.value?`selected`:``}>
                <input
                  type="radio"
                  name="theme"
                  value=${e.value}
                  .checked=${this.theme===e.value}
                  @change=${()=>this.chooseTheme(e.value)}
                />
                ${e.label}
              </label>`)}
          </fieldset>
          <p class="muted small">Automatic follows your device's light or dark setting. Saved in this browser.</p>
        </section>

        <section class="card" data-testid="about">
          <header class="card-head">${N(`lightbulb-auto`)}<h3>About</h3></header>
          ${L([[`App`,e?.app.name??`Smart Occupancy & Lighting`],[`Version`,e?.app.version],[`Phase`,e?`${e.app.phase} (foundation, read-only)`:null],[`Feature settings`,`Not in the App yet: they are migrated module by module`]])}
        </section>

        <section class="card wide">
          <label class="switch-row">
            <input type="checkbox" role="switch" .checked=${this.advanced} @change=${this.toggleAdvanced} data-testid="advanced-toggle" />
            <span>
              <strong>Show advanced settings</strong><br />
              <span class="muted small">Technical details for troubleshooting.</span>
            </span>
          </label>
        </section>
        ${this.advanced?this.renderAdvanced():O}
      </div>
    `}};customElements.define(`sl-settings`,nn);var rn=class extends F{static{this.properties={route:{state:!0},sub:{state:!0},railExpanded:{state:!0},moreOpen:{state:!0}}}constructor(){super(),this.lastHash=window.location.hash,this.onHashChange=()=>{let e=et(),t=tt(),n=this.editorKey(this.route,this.sub);if(n&&n!==this.editorKey(e,t)){let e=ot();if(e&&!window.confirm(e)){history.replaceState(null,``,this.lastHash||`#/rooms`);return}}this.lastHash=window.location.hash;let r=e!==this.route||this.editorKey(e,t)!==n;this.sub=t,r&&(this.route=e,window.scrollTo({top:0}),this.updateComplete.then(()=>this.querySelector(`.page-header h2, .editor-title h2`)?.focus({preventScroll:!0}))),this.railExpanded=!1,this.closeMore()},this.onKey=e=>{e.key===`Escape`&&this.railExpanded&&(this.railExpanded=!1)},this.route=et(),this.sub=tt(),this.railExpanded=!1,this.moreOpen=!1}editorKey(e,t){return e===`rooms`&&t[0]?t[0]:null}connectedCallback(){super.connectedCallback(),window.addEventListener(`hashchange`,this.onHashChange),window.addEventListener(`keydown`,this.onKey)}disconnectedCallback(){super.disconnectedCallback(),window.removeEventListener(`hashchange`,this.onHashChange),window.removeEventListener(`keydown`,this.onKey)}onNav(e,t){e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||(e.preventDefault(),nt(t),this.railExpanded=!1,this.closeMore())}async openMore(){this.moreOpen=!0,await this.updateComplete,this.querySelector(`dialog.more-sheet`)?.showModal()}closeMore(){let e=this.querySelector(`dialog.more-sheet`);e?.open&&e.close(),this.moreOpen=!1}isPending(e){return!Qe.has(e)&&(p.overview?.modules.find(t=>t.key===e)?.status??(e===`rooms`?`gui`:`pending`))===`pending`}navLink(e,t){let n=e.key===this.route,r=this.isPending(e.key);return E`<a
      href="#/${e.key}"
      class="nav-link ${n?`active`:``}"
      aria-current=${n?`page`:O}
      data-nav=${e.key}
      @click=${t=>this.onNav(t,e.key)}
    >
      ${N(e.icon)}
      <span class="label">${e.title}</span>
      ${r?t===`sheet`?I(`pending`,`Pending`):E`<span class="pending-dot" aria-hidden="true"></span><span class="visually-hidden">(migration pending)</span>`:O}
    </a>`}renderBottomNav(){let e=!$e.includes(this.route);return E`<nav class="bottom-nav" aria-label="Main">
      ${P.filter(e=>$e.includes(e.key)).map(e=>{let t=e.key===this.route;return E`<a
          href="#/${e.key}"
          class=${t?`active`:``}
          aria-current=${t?`page`:O}
          data-bottom=${e.key}
          @click=${t=>this.onNav(t,e.key)}
          >${N(e.icon)}<span>${e.title}</span></a
        >`})}
      <button
        type="button"
        class=${e?`active`:``}
        aria-haspopup="dialog"
        data-bottom="more"
        @click=${()=>void this.openMore()}
      >
        ${N(`more`)}<span>More</span>
      </button>
    </nav>`}renderMoreSheet(){return this.moreOpen?E`<dialog
      class="sheet sheet-bottom more-sheet"
      aria-labelledby="more-title"
      @close=${()=>this.moreOpen=!1}
      @click=${e=>e.target.matches(`dialog`)&&this.closeMore()}
    >
      <div class="sheet-inner">
        <header class="sheet-head">
          <h2 id="more-title">All modules</h2>
          <button type="button" class="icon-btn" aria-label="Close" @click=${()=>this.closeMore()}>${N(`close`)}</button>
        </header>
        <nav class="sheet-nav" aria-label="All modules">${P.map(e=>this.navLink(e,`sheet`))}</nav>
      </div>
    </dialog>`:O}renderPage(){switch(this.route){case`dashboard`:return E`<sl-dashboard></sl-dashboard>`;case`diagnostics`:return E`<sl-diagnostics></sl-diagnostics>`;case`settings`:return E`<sl-settings></sl-settings>`;case`rooms`:return this.sub[0]?E`<sl-room-editor .areaId=${this.sub[0]} .tab=${this.sub[1]??`overview`}></sl-room-editor>`:E`<sl-rooms></sl-rooms>`;default:return E`<sl-pending-module .moduleKey=${this.route}></sl-pending-module>`}}render(){let e=p.overview,t=P.find(e=>e.key===this.route)??P[0],n=z(p.link),r=p.link===`offline`,i=r?n:B(e?.home_assistant.connection);return E`
      <a class="skip-link" href="#content" @click=${e=>(e.preventDefault(),this.querySelector(`#content`)?.focus())}
        >Skip to content</a
      >
      <div class="shell ${this.railExpanded?`rail-expanded`:``}">
        <aside class="sidebar" aria-label="Navigation">
          <div class="brand">
            <button
              type="button"
              class="icon-btn rail-toggle"
              aria-label=${this.railExpanded?`Collapse navigation`:`Expand navigation`}
              aria-expanded=${this.railExpanded?`true`:`false`}
              @click=${()=>this.railExpanded=!this.railExpanded}
            >
              ${N(this.railExpanded?`menu-open`:`menu`)}
            </button>
            <span class="brand-mark">${N(`lightbulb-auto`)}</span>
            <span class="brand-text">Smart Lighting</span>
          </div>
          <nav class="nav" aria-label="Modules">${P.map(e=>this.navLink(e,`side`))}</nav>
          <div class="sidebar-foot">
            <span class="muted small">Phase ${e?.app.phase??`A1`} · v${e?.app.version??`…`}</span>
          </div>
        </aside>
        <div class="scrim" @click=${()=>this.railExpanded=!1}></div>
        <div class="main">
          <header class="topbar">
            <span class="topbar-mark">${N(`lightbulb-auto`,22)}</span>
            <h1 class="page-title">${t.title}</h1>
            <span class="topbar-status" data-testid="connection-pill" title=${`App link: ${n.label}`}>
              ${I(i.tone,i.label)}
              <span class="visually-hidden">${r?``:`Home Assistant connection`}</span>
            </span>
          </header>
          <main class="content" id="content" tabindex="-1">${this.renderPage()}</main>
        </div>
        ${this.renderBottomNav()}
      </div>
      ${this.renderMoreSheet()}
    `}};customElements.define(`sl-app`,rn),c(),m.start();