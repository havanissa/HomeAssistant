(function(){let e=document.createElement(`link`).relList;if(e&&e.supports&&e.supports(`modulepreload`))return;for(let e of document.querySelectorAll(`link[rel="modulepreload"]`))n(e);new MutationObserver(e=>{for(let t of e)if(t.type===`childList`)for(let e of t.addedNodes)e.tagName===`LINK`&&e.rel===`modulepreload`&&n(e)}).observe(document,{childList:!0,subtree:!0});function t(e){let t={};return e.integrity&&(t.integrity=e.integrity),e.referrerPolicy&&(t.referrerPolicy=e.referrerPolicy),t.credentials=e.crossOrigin===`use-credentials`?`include`:e.crossOrigin===`anonymous`?`omit`:`same-origin`,t}function n(e){if(e.ep)return;e.ep=!0;let n=t(e);fetch(e.href,n)}})();var e=`smart-lighting.theme`,t=`smart-lighting.advanced`;function n(e){try{return window.localStorage.getItem(e)}catch{return null}}function r(e,t){try{window.localStorage.setItem(e,t)}catch{}}var i=(()=>{let t=n(e);return t===`light`||t===`dark`?t:`auto`})(),a=n(t)===`1`;function o(){return i}function s(t){i=t,r(e,t),c()}function c(){let e=document.documentElement;i===`auto`?e.removeAttribute(`data-theme`):e.setAttribute(`data-theme`,i)}function l(){return a}function u(e){a=e,r(t,e?`1`:`0`)}function d(e){return new URL(e,document.baseURI).toString()}async function f(e){let t=await fetch(d(e),{cache:`no-store`,credentials:`same-origin`});if(!t.ok)throw Error(`HTTP ${t.status}`);return await t.json()}var p=new class{constructor(){this.overview=null,this.link=`connecting`,this.linkError=null,this.lastUpdate=null,this.listeners=new Set}subscribe(e){return this.listeners.add(e),()=>this.listeners.delete(e)}emit(){for(let e of this.listeners)e()}setLink(e,t=null){(e!==this.link||t!==this.linkError)&&(this.link=e,this.linkError=t,this.emit())}setOverview(e){this.overview=e,this.lastUpdate=new Date,this.emit()}apply(e){switch(e.type){case`overview`:this.setOverview(e.overview);return;case`status`:if(!this.overview)return;this.overview={...this.overview,app:e.app,checks:e.checks,home_assistant:{connection:e.connection,info:e.info}};break;case`entities`:{if(!this.overview)return;let t=new Map(e.changed.map(e=>[e.entity_id,e]));this.overview={...this.overview,integration_entities:this.overview.integration_entities.map(e=>t.get(e.entity_id)??e)};break}default:return}this.lastUpdate=new Date,this.emit()}},ee=[1,2,5,10,30],te=1e4,ne=class{constructor(){this.ws=null,this.failures=0}start(){this.refresh(),this.open()}async refresh(){try{p.setOverview(await f(`api/overview`)),p.link===`offline`&&p.setLink(this.ws?.readyState===WebSocket.OPEN?`live`:`polling`)}catch(e){p.link!==`live`&&p.setLink(`offline`,String(e))}}open(){let e=new URL(`api/live`,document.baseURI);e.protocol=e.protocol===`https:`?`wss:`:`ws:`;let t;try{t=new WebSocket(e)}catch{this.scheduleRetry();return}this.ws=t,t.onopen=()=>{this.failures=0,this.stopPolling(),p.setLink(`live`)},t.onmessage=e=>{try{p.apply(JSON.parse(e.data))}catch{}},t.onclose=()=>{this.ws===t&&(this.ws=null),this.scheduleRetry()}}scheduleRetry(){this.failures+=1,this.failures>=2?this.startPolling():p.setLink(`connecting`);let e=ee[Math.min(this.failures-1,ee.length-1)]*1e3;window.clearTimeout(this.retryTimer),this.retryTimer=window.setTimeout(()=>this.open(),e)}startPolling(){this.pollTimer===void 0&&(p.link!==`offline`&&p.setLink(`polling`),this.pollTimer=window.setInterval(()=>void this.refresh(),te),this.refresh())}stopPolling(){window.clearInterval(this.pollTimer),this.pollTimer=void 0}},m=globalThis,h=m.ShadowRoot&&(m.ShadyCSS===void 0||m.ShadyCSS.nativeShadow)&&`adoptedStyleSheets`in Document.prototype&&`replace`in CSSStyleSheet.prototype,re=Symbol(),ie=new WeakMap,ae=class{constructor(e,t,n){if(this._$cssResult$=!0,n!==re)throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");this.cssText=e,this.t=t}get styleSheet(){let e=this.o,t=this.t;if(h&&e===void 0){let n=t!==void 0&&t.length===1;n&&(e=ie.get(t)),e===void 0&&((this.o=e=new CSSStyleSheet).replaceSync(this.cssText),n&&ie.set(t,e))}return e}toString(){return this.cssText}},oe=e=>new ae(typeof e==`string`?e:e+``,void 0,re),se=(e,t)=>{if(h)e.adoptedStyleSheets=t.map(e=>e instanceof CSSStyleSheet?e:e.styleSheet);else for(let n of t){let t=document.createElement(`style`),r=m.litNonce;r!==void 0&&t.setAttribute(`nonce`,r),t.textContent=n.cssText,e.appendChild(t)}},ce=h?e=>e:e=>e instanceof CSSStyleSheet?(e=>{let t=``;for(let n of e.cssRules)t+=n.cssText;return oe(t)})(e):e,{is:le,defineProperty:ue,getOwnPropertyDescriptor:de,getOwnPropertyNames:fe,getOwnPropertySymbols:pe,getPrototypeOf:me}=Object,g=globalThis,he=g.trustedTypes,ge=he?he.emptyScript:``,_e=g.reactiveElementPolyfillSupport,_=(e,t)=>e,v={toAttribute(e,t){switch(t){case Boolean:e=e?ge:null;break;case Object:case Array:e=e==null?e:JSON.stringify(e)}return e},fromAttribute(e,t){let n=e;switch(t){case Boolean:n=e!==null;break;case Number:n=e===null?null:Number(e);break;case Object:case Array:try{n=JSON.parse(e)}catch{n=null}}return n}},ve=(e,t)=>!le(e,t),ye={attribute:!0,type:String,converter:v,reflect:!1,useDefault:!1,hasChanged:ve};Symbol.metadata??=Symbol(`metadata`),g.litPropertyMetadata??=new WeakMap;var y=class extends HTMLElement{static addInitializer(e){this._$Ei(),(this.l??=[]).push(e)}static get observedAttributes(){return this.finalize(),this._$Eh&&[...this._$Eh.keys()]}static createProperty(e,t=ye){if(t.state&&(t.attribute=!1),this._$Ei(),this.prototype.hasOwnProperty(e)&&((t=Object.create(t)).wrapped=!0),this.elementProperties.set(e,t),!t.noAccessor){let n=Symbol(),r=this.getPropertyDescriptor(e,n,t);r!==void 0&&ue(this.prototype,e,r)}}static getPropertyDescriptor(e,t,n){let{get:r,set:i}=de(this.prototype,e)??{get(){return this[t]},set(e){this[t]=e}};return{get:r,set(t){let a=r?.call(this);i?.call(this,t),this.requestUpdate(e,a,n)},configurable:!0,enumerable:!0}}static getPropertyOptions(e){return this.elementProperties.get(e)??ye}static _$Ei(){if(this.hasOwnProperty(_(`elementProperties`)))return;let e=me(this);e.finalize(),e.l!==void 0&&(this.l=[...e.l]),this.elementProperties=new Map(e.elementProperties)}static finalize(){if(this.hasOwnProperty(_(`finalized`)))return;if(this.finalized=!0,this._$Ei(),this.hasOwnProperty(_(`properties`))){let e=this.properties,t=[...fe(e),...pe(e)];for(let n of t)this.createProperty(n,e[n])}let e=this[Symbol.metadata];if(e!==null){let t=litPropertyMetadata.get(e);if(t!==void 0)for(let[e,n]of t)this.elementProperties.set(e,n)}this._$Eh=new Map;for(let[e,t]of this.elementProperties){let n=this._$Eu(e,t);n!==void 0&&this._$Eh.set(n,e)}this.elementStyles=this.finalizeStyles(this.styles)}static finalizeStyles(e){let t=[];if(Array.isArray(e)){let n=new Set(e.flat(1/0).reverse());for(let e of n)t.unshift(ce(e))}else e!==void 0&&t.push(ce(e));return t}static _$Eu(e,t){let n=t.attribute;return!1===n?void 0:typeof n==`string`?n:typeof e==`string`?e.toLowerCase():void 0}constructor(){super(),this._$Ep=void 0,this.isUpdatePending=!1,this.hasUpdated=!1,this._$Em=null,this._$Ev()}_$Ev(){this._$ES=new Promise(e=>this.enableUpdating=e),this._$AL=new Map,this._$E_(),this.requestUpdate(),this.constructor.l?.forEach(e=>e(this))}addController(e){(this._$EO??=new Set).add(e),this.renderRoot!==void 0&&this.isConnected&&e.hostConnected?.()}removeController(e){this._$EO?.delete(e)}_$E_(){let e=new Map,t=this.constructor.elementProperties;for(let n of t.keys())this.hasOwnProperty(n)&&(e.set(n,this[n]),delete this[n]);e.size>0&&(this._$Ep=e)}createRenderRoot(){let e=this.shadowRoot??this.attachShadow(this.constructor.shadowRootOptions);return se(e,this.constructor.elementStyles),e}connectedCallback(){this.renderRoot??=this.createRenderRoot(),this.enableUpdating(!0),this._$EO?.forEach(e=>e.hostConnected?.())}enableUpdating(e){}disconnectedCallback(){this._$EO?.forEach(e=>e.hostDisconnected?.())}attributeChangedCallback(e,t,n){this._$AK(e,n)}_$ET(e,t){let n=this.constructor.elementProperties.get(e),r=this.constructor._$Eu(e,n);if(r!==void 0&&!0===n.reflect){let i=(n.converter?.toAttribute===void 0?v:n.converter).toAttribute(t,n.type);this._$Em=e,i==null?this.removeAttribute(r):this.setAttribute(r,i),this._$Em=null}}_$AK(e,t){let n=this.constructor,r=n._$Eh.get(e);if(r!==void 0&&this._$Em!==r){let e=n.getPropertyOptions(r),i=typeof e.converter==`function`?{fromAttribute:e.converter}:e.converter?.fromAttribute===void 0?v:e.converter;this._$Em=r;let a=i.fromAttribute(t,e.type);this[r]=a??this._$Ej?.get(r)??a,this._$Em=null}}requestUpdate(e,t,n,r=!1,i){if(e!==void 0){let a=this.constructor;if(!1===r&&(i=this[e]),n??=a.getPropertyOptions(e),!((n.hasChanged??ve)(i,t)||n.useDefault&&n.reflect&&i===this._$Ej?.get(e)&&!this.hasAttribute(a._$Eu(e,n))))return;this.C(e,t,n)}!1===this.isUpdatePending&&(this._$ES=this._$EP())}C(e,t,{useDefault:n,reflect:r,wrapped:i},a){n&&!(this._$Ej??=new Map).has(e)&&(this._$Ej.set(e,a??t??this[e]),!0!==i||a!==void 0)||(this._$AL.has(e)||(this.hasUpdated||n||(t=void 0),this._$AL.set(e,t)),!0===r&&this._$Em!==e&&(this._$Eq??=new Set).add(e))}async _$EP(){this.isUpdatePending=!0;try{await this._$ES}catch(e){Promise.reject(e)}let e=this.scheduleUpdate();return e!=null&&await e,!this.isUpdatePending}scheduleUpdate(){return this.performUpdate()}performUpdate(){if(!this.isUpdatePending)return;if(!this.hasUpdated){if(this.renderRoot??=this.createRenderRoot(),this._$Ep){for(let[e,t]of this._$Ep)this[e]=t;this._$Ep=void 0}let e=this.constructor.elementProperties;if(e.size>0)for(let[t,n]of e){let{wrapped:e}=n,r=this[t];!0!==e||this._$AL.has(t)||r===void 0||this.C(t,void 0,n,r)}}let e=!1,t=this._$AL;try{e=this.shouldUpdate(t),e?(this.willUpdate(t),this._$EO?.forEach(e=>e.hostUpdate?.()),this.update(t)):this._$EM()}catch(t){throw e=!1,this._$EM(),t}e&&this._$AE(t)}willUpdate(e){}_$AE(e){this._$EO?.forEach(e=>e.hostUpdated?.()),this.hasUpdated||(this.hasUpdated=!0,this.firstUpdated(e)),this.updated(e)}_$EM(){this._$AL=new Map,this.isUpdatePending=!1}get updateComplete(){return this.getUpdateComplete()}getUpdateComplete(){return this._$ES}shouldUpdate(e){return!0}update(e){this._$Eq&&=this._$Eq.forEach(e=>this._$ET(e,this[e])),this._$EM()}updated(e){}firstUpdated(e){}};y.elementStyles=[],y.shadowRootOptions={mode:`open`},y[_(`elementProperties`)]=new Map,y[_(`finalized`)]=new Map,_e?.({ReactiveElement:y}),(g.reactiveElementVersions??=[]).push(`2.1.2`);var b=globalThis,be=e=>e,x=b.trustedTypes,xe=x?x.createPolicy(`lit-html`,{createHTML:e=>e}):void 0,Se=`$lit$`,S=`lit$${Math.random().toFixed(9).slice(2)}$`,Ce=`?`+S,we=`<${Ce}>`,C=document,w=()=>C.createComment(``),T=e=>e===null||typeof e!=`object`&&typeof e!=`function`,E=Array.isArray,Te=e=>E(e)||typeof e?.[Symbol.iterator]==`function`,D=`[ 	
\f\r]`,O=/<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g,Ee=/-->/g,De=/>/g,k=RegExp(`>|${D}(?:([^\\s"'>=/]+)(${D}*=${D}*(?:[^ \t\n\f\r"'\`<>=]|("|')|))|$)`,`g`),Oe=/'/g,ke=/"/g,Ae=/^(?:script|style|textarea|title)$/i,je=e=>(t,...n)=>({_$litType$:e,strings:t,values:n}),A=je(1),Me=je(2),j=Symbol.for(`lit-noChange`),M=Symbol.for(`lit-nothing`),Ne=new WeakMap,N=C.createTreeWalker(C,129);function Pe(e,t){if(!E(e)||!e.hasOwnProperty(`raw`))throw Error(`invalid template strings array`);return xe===void 0?t:xe.createHTML(t)}var Fe=(e,t)=>{let n=e.length-1,r=[],i,a=t===2?`<svg>`:t===3?`<math>`:``,o=O;for(let t=0;t<n;t++){let n=e[t],s,c,l=-1,u=0;for(;u<n.length&&(o.lastIndex=u,c=o.exec(n),c!==null);)u=o.lastIndex,o===O?c[1]===`!--`?o=Ee:c[1]===void 0?c[2]===void 0?c[3]!==void 0&&(o=k):(Ae.test(c[2])&&(i=RegExp(`</`+c[2],`g`)),o=k):o=De:o===k?c[0]===`>`?(o=i??O,l=-1):c[1]===void 0?l=-2:(l=o.lastIndex-c[2].length,s=c[1],o=c[3]===void 0?k:c[3]===`"`?ke:Oe):o===ke||o===Oe?o=k:o===Ee||o===De?o=O:(o=k,i=void 0);let d=o===k&&e[t+1].startsWith(`/>`)?` `:``;a+=o===O?n+we:l>=0?(r.push(s),n.slice(0,l)+Se+n.slice(l)+S+d):n+S+(l===-2?t:d)}return[Pe(e,a+(e[n]||`<?>`)+(t===2?`</svg>`:t===3?`</math>`:``)),r]},P=class e{constructor({strings:t,_$litType$:n},r){let i;this.parts=[];let a=0,o=0,s=t.length-1,c=this.parts,[l,u]=Fe(t,n);if(this.el=e.createElement(l,r),N.currentNode=this.el.content,n===2||n===3){let e=this.el.content.firstChild;e.replaceWith(...e.childNodes)}for(;(i=N.nextNode())!==null&&c.length<s;){if(i.nodeType===1){if(i.hasAttributes())for(let e of i.getAttributeNames())if(e.endsWith(Se)){let t=u[o++],n=i.getAttribute(e).split(S),r=/([.?@])?(.*)/.exec(t);c.push({type:1,index:a,name:r[2],strings:n,ctor:r[1]===`.`?Le:r[1]===`?`?Re:r[1]===`@`?ze:L}),i.removeAttribute(e)}else e.startsWith(S)&&(c.push({type:6,index:a}),i.removeAttribute(e));if(Ae.test(i.tagName)){let e=i.textContent.split(S),t=e.length-1;if(t>0){i.textContent=x?x.emptyScript:``;for(let n=0;n<t;n++)i.append(e[n],w()),N.nextNode(),c.push({type:2,index:++a});i.append(e[t],w())}}}else if(i.nodeType===8){if(i.data===Ce)c.push({type:2,index:a});else{let e=-1;for(;(e=i.data.indexOf(S,e+1))!==-1;)c.push({type:7,index:a}),e+=S.length-1}}a++}}static createElement(e,t){let n=C.createElement(`template`);return n.innerHTML=e,n}};function F(e,t,n=e,r){if(t===j)return t;let i=r===void 0?n._$Cl:n._$Co?.[r],a=T(t)?void 0:t._$litDirective$;return i?.constructor!==a&&(i?._$AO?.(!1),a===void 0?i=void 0:(i=new a(e),i._$AT(e,n,r)),r===void 0?n._$Cl=i:(n._$Co??=[])[r]=i),i!==void 0&&(t=F(e,i._$AS(e,t.values),i,r)),t}var Ie=class{constructor(e,t){this._$AV=[],this._$AN=void 0,this._$AD=e,this._$AM=t}get parentNode(){return this._$AM.parentNode}get _$AU(){return this._$AM._$AU}u(e){let{el:{content:t},parts:n}=this._$AD,r=(e?.creationScope??C).importNode(t,!0);N.currentNode=r;let i=N.nextNode(),a=0,o=0,s=n[0];for(;s!==void 0;){if(a===s.index){let t;s.type===2?t=new I(i,i.nextSibling,this,e):s.type===1?t=new s.ctor(i,s.name,s.strings,this,e):s.type===6&&(t=new Be(i,this,e)),this._$AV.push(t),s=n[++o]}a!==s?.index&&(i=N.nextNode(),a++)}return N.currentNode=C,r}p(e){let t=0;for(let n of this._$AV)n!==void 0&&(n.strings===void 0?n._$AI(e[t]):(n._$AI(e,n,t),t+=n.strings.length-2)),t++}},I=class e{get _$AU(){return this._$AM?._$AU??this._$Cv}constructor(e,t,n,r){this.type=2,this._$AH=M,this._$AN=void 0,this._$AA=e,this._$AB=t,this._$AM=n,this.options=r,this._$Cv=r?.isConnected??!0}get parentNode(){let e=this._$AA.parentNode,t=this._$AM;return t!==void 0&&e?.nodeType===11&&(e=t.parentNode),e}get startNode(){return this._$AA}get endNode(){return this._$AB}_$AI(e,t=this){e=F(this,e,t),T(e)?e===M||e==null||e===``?(this._$AH!==M&&this._$AR(),this._$AH=M):e!==this._$AH&&e!==j&&this._(e):e._$litType$===void 0?e.nodeType===void 0?Te(e)?this.k(e):this._(e):this.T(e):this.$(e)}O(e){return this._$AA.parentNode.insertBefore(e,this._$AB)}T(e){this._$AH!==e&&(this._$AR(),this._$AH=this.O(e))}_(e){this._$AH!==M&&T(this._$AH)?this._$AA.nextSibling.data=e:this.T(C.createTextNode(e)),this._$AH=e}$(e){let{values:t,_$litType$:n}=e,r=typeof n==`number`?this._$AC(e):(n.el===void 0&&(n.el=P.createElement(Pe(n.h,n.h[0]),this.options)),n);if(this._$AH?._$AD===r)this._$AH.p(t);else{let e=new Ie(r,this),n=e.u(this.options);e.p(t),this.T(n),this._$AH=e}}_$AC(e){let t=Ne.get(e.strings);return t===void 0&&Ne.set(e.strings,t=new P(e)),t}k(t){E(this._$AH)||(this._$AH=[],this._$AR());let n=this._$AH,r,i=0;for(let a of t)i===n.length?n.push(r=new e(this.O(w()),this.O(w()),this,this.options)):r=n[i],r._$AI(a),i++;i<n.length&&(this._$AR(r&&r._$AB.nextSibling,i),n.length=i)}_$AR(e=this._$AA.nextSibling,t){for(this._$AP?.(!1,!0,t);e!==this._$AB;){let t=be(e).nextSibling;be(e).remove(),e=t}}setConnected(e){this._$AM===void 0&&(this._$Cv=e,this._$AP?.(e))}},L=class{get tagName(){return this.element.tagName}get _$AU(){return this._$AM._$AU}constructor(e,t,n,r,i){this.type=1,this._$AH=M,this._$AN=void 0,this.element=e,this.name=t,this._$AM=r,this.options=i,n.length>2||n[0]!==``||n[1]!==``?(this._$AH=Array(n.length-1).fill(new String),this.strings=n):this._$AH=M}_$AI(e,t=this,n,r){let i=this.strings,a=!1;if(i===void 0)e=F(this,e,t,0),a=!T(e)||e!==this._$AH&&e!==j,a&&(this._$AH=e);else{let r=e,o,s;for(e=i[0],o=0;o<i.length-1;o++)s=F(this,r[n+o],t,o),s===j&&(s=this._$AH[o]),a||=!T(s)||s!==this._$AH[o],s===M?e=M:e!==M&&(e+=(s??``)+i[o+1]),this._$AH[o]=s}a&&!r&&this.j(e)}j(e){e===M?this.element.removeAttribute(this.name):this.element.setAttribute(this.name,e??``)}},Le=class extends L{constructor(){super(...arguments),this.type=3}j(e){this.element[this.name]=e===M?void 0:e}},Re=class extends L{constructor(){super(...arguments),this.type=4}j(e){this.element.toggleAttribute(this.name,!!e&&e!==M)}},ze=class extends L{constructor(e,t,n,r,i){super(e,t,n,r,i),this.type=5}_$AI(e,t=this){if((e=F(this,e,t,0)??M)===j)return;let n=this._$AH,r=e===M&&n!==M||e.capture!==n.capture||e.once!==n.once||e.passive!==n.passive,i=e!==M&&(n===M||r);r&&this.element.removeEventListener(this.name,this,n),i&&this.element.addEventListener(this.name,this,e),this._$AH=e}handleEvent(e){typeof this._$AH==`function`?this._$AH.call(this.options?.host??this.element,e):this._$AH.handleEvent(e)}},Be=class{constructor(e,t,n){this.element=e,this.type=6,this._$AN=void 0,this._$AM=t,this.options=n}get _$AU(){return this._$AM._$AU}_$AI(e){F(this,e)}},Ve=b.litHtmlPolyfillSupport;Ve?.(P,I),(b.litHtmlVersions??=[]).push(`3.3.3`);var He=(e,t,n)=>{let r=n?.renderBefore??t,i=r._$litPart$;if(i===void 0){let e=n?.renderBefore??null;r._$litPart$=i=new I(t.insertBefore(w(),e),e,void 0,n??{})}return i._$AI(e),i},R=globalThis,z=class extends y{constructor(){super(...arguments),this.renderOptions={host:this},this._$Do=void 0}createRenderRoot(){let e=super.createRenderRoot();return this.renderOptions.renderBefore??=e.firstChild,e}update(e){let t=this.render();this.hasUpdated||(this.renderOptions.isConnected=this.isConnected),super.update(e),this._$Do=He(t,this.renderRoot,this.renderOptions)}connectedCallback(){super.connectedCallback(),this._$Do?.setConnected(!0)}disconnectedCallback(){super.disconnectedCallback(),this._$Do?.setConnected(!1)}render(){return j}};z._$litElement$=!0,z.finalized=!0,R.litElementHydrateSupport?.({LitElement:z});var Ue=R.litElementPolyfillSupport;Ue?.({LitElement:z}),(R.litElementVersions??=[]).push(`4.2.2`);var We={"view-dashboard":`M13,3V9H21V3M13,21H21V11H13M3,21H11V15H3M3,13H11V3H3V13Z`,"floor-plan":`M10,5V10H9V5H5V13H9V12H10V17H9V14H5V19H12V17H13V19H19V17H21V21H3V3H21V15H19V10H13V15H12V9H19V5H10Z`,"motion-sensor":`M10,0.2C9,0.2 8.2,1 8.2,2C8.2,3 9,3.8 10,3.8C11,3.8 11.8,3 11.8,2C11.8,1 11,0.2 10,0.2M15.67,1A7.33,7.33 0 0,0 23,8.33V7A6,6 0 0,1 17,1H15.67M18.33,1C18.33,3.58 20.42,5.67 23,5.67V4.33C21.16,4.33 19.67,2.84 19.67,1H18.33M21,1A2,2 0 0,0 23,3V1H21M7.92,4.03C7.75,4.03 7.58,4.06 7.42,4.11L2,5.8V11H3.8V7.33L5.91,6.67L2,22H3.8L6.67,13.89L9,17V22H10.8V15.59L8.31,11.05L9.04,8.18L10.12,10H15V8.2H11.38L9.38,4.87C9.08,4.37 8.54,4.03 7.92,4.03Z`,"lightbulb-group":`M15 14V16A1 1 0 0 1 14 17H10A1 1 0 0 1 9 16V14A5 5 0 1 1 15 14M14 18H10V19A1 1 0 0 0 11 20H13A1 1 0 0 0 14 19M7 19V18H5V19A1 1 0 0 0 6 20H7.17A2.93 2.93 0 0 1 7 19M5 10A6.79 6.79 0 0 1 5.68 7A4 4 0 0 0 4 14.45V16A1 1 0 0 0 5 17H7V14.88A6.92 6.92 0 0 1 5 10M17 18V19A2.93 2.93 0 0 1 16.83 20H18A1 1 0 0 0 19 19V18M18.32 7A6.79 6.79 0 0 1 19 10A6.92 6.92 0 0 1 17 14.88V17H19A1 1 0 0 0 20 16V14.45A4 4 0 0 0 18.32 7Z`,palette:`M17.5,12A1.5,1.5 0 0,1 16,10.5A1.5,1.5 0 0,1 17.5,9A1.5,1.5 0 0,1 19,10.5A1.5,1.5 0 0,1 17.5,12M14.5,8A1.5,1.5 0 0,1 13,6.5A1.5,1.5 0 0,1 14.5,5A1.5,1.5 0 0,1 16,6.5A1.5,1.5 0 0,1 14.5,8M9.5,8A1.5,1.5 0 0,1 8,6.5A1.5,1.5 0 0,1 9.5,5A1.5,1.5 0 0,1 11,6.5A1.5,1.5 0 0,1 9.5,8M6.5,12A1.5,1.5 0 0,1 5,10.5A1.5,1.5 0 0,1 6.5,9A1.5,1.5 0 0,1 8,10.5A1.5,1.5 0 0,1 6.5,12M12,3A9,9 0 0,0 3,12A9,9 0 0,0 12,21A1.5,1.5 0 0,0 13.5,19.5C13.5,19.11 13.35,18.76 13.11,18.5C12.88,18.23 12.73,17.88 12.73,17.5A1.5,1.5 0 0,1 14.23,16H16A5,5 0 0,0 21,11C21,6.58 16.97,3 12,3Z`,"light-switch":`M8 6V18H16V6H8M14 10H10V8H14V10M19.4 1.6C19 1.2 18.5 1 18 1H6C5.5 1 5 1.2 4.6 1.6C4.2 2 4 2.5 4 3V21C4 21.5 4.2 22 4.6 22.4C5 22.8 5.5 23 6 23H18C18.5 23 19 22.8 19.4 22.4C19.8 22 20 21.5 20 21V3C20 2.5 19.8 2 19.4 1.6M18 21H6V3H18V21Z`,curtains:`M23 3H1V1H23V3M2 22H6C6 19 4 17 4 17C10 13 11 4 11 4H2V22M22 4H13C13 4 14 13 20 17C20 17 18 19 18 22H22V4Z`,"crystal-ball":`M9.38,8.38L11.5,9.34L13.62,8.38L12.66,10.5L13.62,12.62L11.5,11.66L9.38,12.62L10.34,10.5L9.38,8.38M16.5,2.5L17.59,5.41L20.5,6.5L17.59,7.59L16.5,10.5L15.41,7.59L12.5,6.5L15.41,5.41L16.5,2.5M6,19H7V18A1,1 0 0,1 8,17H8.26C6,15.7 4.5,13.28 4.5,10.5A7.5,7.5 0 0,1 12,3C13.05,3 14.05,3.22 14.96,3.61L14.59,4.59L13.17,5.12C12.79,5.04 12.4,5 12,5A5.5,5.5 0 0,0 6.5,10.5A5.5,5.5 0 0,0 12,16C14.91,16 17.3,13.73 17.5,10.87L18.41,8.41L19.12,8.14C19.37,8.88 19.5,9.68 19.5,10.5C19.5,13.28 18,15.7 15.74,17H16A1,1 0 0,1 17,18V19H18A2,2 0 0,1 20,21V22H4V21A2,2 0 0,1 6,19Z`,stethoscope:`M19,8C19.56,8 20,8.43 20,9A1,1 0 0,1 19,10C18.43,10 18,9.55 18,9C18,8.43 18.43,8 19,8M2,2V11C2,13.96 4.19,16.5 7.14,16.91C7.76,19.92 10.42,22 13.5,22A6.5,6.5 0 0,0 20,15.5V11.81C21.16,11.39 22,10.29 22,9A3,3 0 0,0 19,6A3,3 0 0,0 16,9C16,10.29 16.84,11.4 18,11.81V15.41C18,17.91 16,19.91 13.5,19.91C11.5,19.91 9.82,18.7 9.22,16.9C12,16.3 14,13.8 14,11V2H10V5H12V11A4,4 0 0,1 8,15A4,4 0 0,1 4,11V5H6V2H2Z`,cog:`M12,15.5A3.5,3.5 0 0,1 8.5,12A3.5,3.5 0 0,1 12,8.5A3.5,3.5 0 0,1 15.5,12A3.5,3.5 0 0,1 12,15.5M19.43,12.97C19.47,12.65 19.5,12.33 19.5,12C19.5,11.67 19.47,11.34 19.43,11L21.54,9.37C21.73,9.22 21.78,8.95 21.66,8.73L19.66,5.27C19.54,5.05 19.27,4.96 19.05,5.05L16.56,6.05C16.04,5.66 15.5,5.32 14.87,5.07L14.5,2.42C14.46,2.18 14.25,2 14,2H10C9.75,2 9.54,2.18 9.5,2.42L9.13,5.07C8.5,5.32 7.96,5.66 7.44,6.05L4.95,5.05C4.73,4.96 4.46,5.05 4.34,5.27L2.34,8.73C2.21,8.95 2.27,9.22 2.46,9.37L4.57,11C4.53,11.34 4.5,11.67 4.5,12C4.5,12.33 4.53,12.65 4.57,12.97L2.46,14.63C2.27,14.78 2.21,15.05 2.34,15.27L4.34,18.73C4.46,18.95 4.73,19.03 4.95,18.95L7.44,17.94C7.96,18.34 8.5,18.68 9.13,18.93L9.5,21.58C9.54,21.82 9.75,22 10,22H14C14.25,22 14.46,21.82 14.5,21.58L14.87,18.93C15.5,18.67 16.04,18.34 16.56,17.94L19.05,18.95C19.27,19.03 19.54,18.95 19.66,18.73L21.66,15.27C21.78,15.05 21.73,14.78 21.54,14.63L19.43,12.97Z`,"lightbulb-auto":`M9 2C5.13 2 2 5.13 2 9C2 11.38 3.19 13.47 5 14.74V17C5 17.55 5.45 18 6 18H12C12.55 18 13 17.55 13 17V14.74C14.81 13.47 16 11.38 16 9C16 5.13 12.87 2 9 2M6 21C6 21.55 6.45 22 7 22H11C11.55 22 12 21.55 12 21V20H6V21M19 13H17L13.8 22H15.7L16.4 20H19.6L20.3 22H22.2L19 13M16.85 18.65L18 15L19.15 18.65H16.85Z`,menu:`M3,6H21V8H3V6M3,11H21V13H3V11M3,16H21V18H3V16Z`,close:`M19,6.41L17.59,5L12,10.59L6.41,5L5,6.41L10.59,12L5,17.59L6.41,19L12,13.41L17.59,19L19,17.59L13.41,12L19,6.41Z`,ok:`M12 2C6.5 2 2 6.5 2 12S6.5 22 12 22 22 17.5 22 12 17.5 2 12 2M10 17L5 12L6.41 10.59L10 14.17L17.59 6.58L19 8L10 17Z`,warning:`M13 14H11V9H13M13 18H11V16H13M1 21H23L12 2L1 21Z`,error:`M13,13H11V7H13M13,17H11V15H13M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z`,info:`M13,9H11V7H13M13,17H11V11H13M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z`,"home-assistant":`M21.8,13H20V21H13V17.67L15.79,14.88L16.5,15C17.66,15 18.6,14.06 18.6,12.9C18.6,11.74 17.66,10.8 16.5,10.8A2.1,2.1 0 0,0 14.4,12.9L14.5,13.61L13,15.13V9.65C13.66,9.29 14.1,8.6 14.1,7.8A2.1,2.1 0 0,0 12,5.7A2.1,2.1 0 0,0 9.9,7.8C9.9,8.6 10.34,9.29 11,9.65V15.13L9.5,13.61L9.6,12.9A2.1,2.1 0 0,0 7.5,10.8A2.1,2.1 0 0,0 5.4,12.9A2.1,2.1 0 0,0 7.5,15L8.21,14.88L11,17.67V21H4V13H2.25C1.83,13 1.42,13 1.42,12.79C1.43,12.57 1.85,12.15 2.28,11.72L11,3C11.33,2.67 11.67,2.33 12,2.33C12.33,2.33 12.67,2.67 13,3L17,7V6H19V9L21.78,11.78C22.18,12.18 22.59,12.59 22.6,12.8C22.6,13 22.2,13 21.8,13M7.5,12A0.9,0.9 0 0,1 8.4,12.9A0.9,0.9 0 0,1 7.5,13.8A0.9,0.9 0 0,1 6.6,12.9A0.9,0.9 0 0,1 7.5,12M16.5,12C17,12 17.4,12.4 17.4,12.9C17.4,13.4 17,13.8 16.5,13.8A0.9,0.9 0 0,1 15.6,12.9A0.9,0.9 0 0,1 16.5,12M12,6.9C12.5,6.9 12.9,7.3 12.9,7.8C12.9,8.3 12.5,8.7 12,8.7C11.5,8.7 11.1,8.3 11.1,7.8C11.1,7.3 11.5,6.9 12,6.9Z`,puzzle:`M20.5,11H19V7C19,5.89 18.1,5 17,5H13V3.5A2.5,2.5 0 0,0 10.5,1A2.5,2.5 0 0,0 8,3.5V5H4A2,2 0 0,0 2,7V10.8H3.5C5,10.8 6.2,12 6.2,13.5C6.2,15 5,16.2 3.5,16.2H2V20A2,2 0 0,0 4,22H7.8V20.5C7.8,19 9,17.8 10.5,17.8C12,17.8 13.2,19 13.2,20.5V22H17A2,2 0 0,0 19,20V16H20.5A2.5,2.5 0 0,0 23,13.5A2.5,2.5 0 0,0 20.5,11Z`,download:`M5,20H19V18H5M19,9H15V3H9V9H5L12,16L19,9Z`,refresh:`M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z`,"chevron-right":`M8.59,16.58L13.17,12L8.59,7.41L10,6L16,12L10,18L8.59,16.58Z`,"shield-check":`M10,17L6,13L7.41,11.59L10,14.17L16.59,7.58L18,9M12,1L3,5V11C3,16.55 6.84,21.74 12,23C17.16,21.74 21,16.55 21,11V5L12,1Z`,pending:`M13,2.03V2.05L13,4.05C17.39,4.59 20.5,8.58 19.96,12.97C19.5,16.61 16.64,19.5 13,19.93V21.93C18.5,21.38 22.5,16.5 21.95,11C21.5,6.25 17.73,2.5 13,2.03M11,2.06C9.05,2.25 7.19,3 5.67,4.26L7.1,5.74C8.22,4.84 9.57,4.26 11,4.06V2.06M4.26,5.67C3,7.19 2.25,9.04 2.05,11H4.05C4.24,9.58 4.8,8.23 5.69,7.1L4.26,5.67M2.06,13C2.26,14.96 3.03,16.81 4.27,18.33L5.69,16.9C4.81,15.77 4.24,14.42 4.06,13H2.06M7.1,18.37L5.67,19.74C7.18,21 9.04,21.79 11,22V20C9.58,19.82 8.23,19.25 7.1,18.37M12.5,7V12.25L17,14.92L16.25,16.15L11,13V7H12.5Z`,search:`M9.5,3A6.5,6.5 0 0,1 16,9.5C16,11.11 15.41,12.59 14.44,13.73L14.71,14H15.5L20.5,19L19,20.5L14,15.5V14.71L13.73,14.44C12.59,15.41 11.11,16 9.5,16A6.5,6.5 0 0,1 3,9.5A6.5,6.5 0 0,1 9.5,3M9.5,5C7,5 5,7 5,9.5C5,12 7,14 9.5,14C12,14 14,12 14,9.5C14,7 12,5 9.5,5Z`,more:`M16,12A2,2 0 0,1 18,10A2,2 0 0,1 20,12A2,2 0 0,1 18,14A2,2 0 0,1 16,12M10,12A2,2 0 0,1 12,10A2,2 0 0,1 14,12A2,2 0 0,1 12,14A2,2 0 0,1 10,12M4,12A2,2 0 0,1 6,10A2,2 0 0,1 8,12A2,2 0 0,1 6,14A2,2 0 0,1 4,12Z`,copy:`M19,21H8V7H19M19,5H8A2,2 0 0,0 6,7V21A2,2 0 0,0 8,23H19A2,2 0 0,0 21,21V7A2,2 0 0,0 19,5M16,1H4A2,2 0 0,0 2,3V17H4V3H16V1Z`,json:`M5,3H7V5H5V10A2,2 0 0,1 3,12A2,2 0 0,1 5,14V19H7V21H5C3.93,20.73 3,20.1 3,19V15A2,2 0 0,0 1,13H0V11H1A2,2 0 0,0 3,9V5A2,2 0 0,1 5,3M19,3A2,2 0 0,1 21,5V9A2,2 0 0,0 23,11H24V13H23A2,2 0 0,0 21,15V19A2,2 0 0,1 19,21H17V19H19V14A2,2 0 0,1 21,12A2,2 0 0,1 19,10V5H17V3H19M12,15A1,1 0 0,1 13,16A1,1 0 0,1 12,17A1,1 0 0,1 11,16A1,1 0 0,1 12,15M8,15A1,1 0 0,1 9,16A1,1 0 0,1 8,17A1,1 0 0,1 7,16A1,1 0 0,1 8,15M16,15A1,1 0 0,1 17,16A1,1 0 0,1 16,17A1,1 0 0,1 15,16A1,1 0 0,1 16,15Z`,"menu-open":`M21,15.61L19.59,17L14.58,12L19.59,7L21,8.39L17.44,12L21,15.61M3,6H16V8H3V6M3,13V11H13V13H3M3,18V16H16V18H3Z`};function B(e,t=24){return Me`<svg class="icon" width=${t} height=${t} viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d=${We[e]??`M20.5,11H19V7C19,5.89 18.1,5 17,5H13V3.5A2.5,2.5 0 0,0 10.5,1A2.5,2.5 0 0,0 8,3.5V5H4A2,2 0 0,0 2,7V10.8H3.5C5,10.8 6.2,12 6.2,13.5C6.2,15 5,16.2 3.5,16.2H2V20A2,2 0 0,0 4,22H7.8V20.5C7.8,19 9,17.8 10.5,17.8C12,17.8 13.2,19 13.2,20.5V22H17A2,2 0 0,0 19,20V16H20.5A2.5,2.5 0 0,0 23,13.5A2.5,2.5 0 0,0 20.5,11Z`}></path></svg>`}var V=[{key:`dashboard`,title:`Dashboard`,icon:`view-dashboard`},{key:`rooms`,title:`Rooms`,icon:`floor-plan`},{key:`presence`,title:`Presence`,icon:`motion-sensor`},{key:`lighting`,title:`Lighting`,icon:`lightbulb-group`},{key:`moods`,title:`Moods`,icon:`palette`},{key:`switches`,title:`Switches`,icon:`light-switch`},{key:`curtains`,title:`Curtains`,icon:`curtains`},{key:`predictions`,title:`Predictions`,icon:`crystal-ball`},{key:`diagnostics`,title:`Diagnostics`,icon:`stethoscope`},{key:`settings`,title:`Settings`,icon:`cog`}],Ge=new Set([`dashboard`,`diagnostics`,`settings`]),Ke=[`dashboard`,`rooms`,`lighting`,`moods`];function H(){let e=window.location.hash.replace(/^#\/?/,``).split(/[/?]/)[0];return V.some(t=>t.key===e)?e:`dashboard`}function U(e){H()===e&&window.location.hash||(window.location.hash=`/${e}`)}var W=class extends z{constructor(...e){super(...e),this.unsubscribe=null}createRenderRoot(){return this}connectedCallback(){super.connectedCallback(),this.unsubscribe=p.subscribe(()=>this.requestUpdate())}disconnectedCallback(){super.disconnectedCallback(),this.unsubscribe?.(),this.unsubscribe=null}};function G(e,t){return A`<span class="chip chip-${e}"><span class="dot" aria-hidden="true"></span><span class="chip-label">${t}</span></span>`}function qe(e){return A`<span class="status-icon tone-${e}" role="img" aria-label=${e}>${B(e,20)}</span>`}function K(e){return A`<dl class="kv">
    ${e.map(e=>e?A`<dt>${e[0]}</dt>
            <dd>${e[1]===null||e[1]===void 0||e[1]===``?`—`:e[1]}</dd>`:M)}
  </dl>`}function q(e,t,n=M){return A`<div class="page-header">
    <div>
      <h2 tabindex="-1">${e}</h2>
      <p class="muted">${t}</p>
    </div>
    ${n}
  </div>`}function J(e){switch(e){case`live`:return{tone:`ok`,label:`Live`};case`polling`:return{tone:`warning`,label:`Updating every 10 s`};case`offline`:return{tone:`error`,label:`App unreachable`};default:return{tone:`neutral`,label:`Connecting…`}}}function Y(e){switch(e?.state){case`connected`:return{tone:`ok`,label:`Connected`};case`connecting`:return{tone:`warning`,label:`Connecting…`};case`disconnected`:return{tone:`warning`,label:`Reconnecting…`};case`auth_failed`:return{tone:`error`,label:`Access refused`};case`not_configured`:return{tone:`error`,label:`No credentials`};default:return{tone:`neutral`,label:`Unknown`}}}function Je(e){switch(e?.status){case`loaded`:return{tone:`ok`,label:`Detected · running`};case`installed`:return{tone:`info`,label:`Installed · not set up`};case`not_running`:return{tone:`warning`,label:`Set up · not running`};case`not_installed`:return{tone:`neutral`,label:`Not installed`};default:return{tone:`neutral`,label:`Not checked yet`}}}function Ye(){return A`<div class="card loading" role="status">
    <span class="spinner" aria-hidden="true"></span>
    ${p.link===`offline`?A`<span>The App backend cannot be reached. Retrying…</span>`:A`<span>Loading…</span>`}
  </div>`}var Xe=new Intl.DateTimeFormat(void 0,{hour:`2-digit`,minute:`2-digit`,hourCycle:`h23`}),Ze=new Intl.DateTimeFormat(void 0,{day:`numeric`,month:`short`,hour:`2-digit`,minute:`2-digit`,hourCycle:`h23`});function X(e){if(!e)return`—`;let t=new Date(e);if(Number.isNaN(t.getTime()))return`—`;let n=new Date;return t.toDateString()===n.toDateString()?Xe.format(t):Ze.format(t)}function Qe(e){if(e==null||e<0)return`—`;if(e<60)return`${Math.floor(e)} s`;let t=Math.floor(e/60);if(t<60)return`${t} min`;let n=Math.floor(t/60);return n<24?`${n} h ${t%60} min`:`${Math.floor(n/24)} d ${n%24} h`}function $e(e,t){return e===null?`Not available`:e===`unavailable`?`Unavailable`:e===`unknown`?`Unknown`:t?`${e} ${t}`:e}function et(e,t,n){return`${e} ${e===1?t:n}`}var tt={ok:{tone:`ok`,label:`Healthy`},warning:{tone:`warning`,label:`Degraded`},error:{tone:`error`,label:`Problem`}},nt=class extends W{renderConnection(e){let t=e.home_assistant.connection,n=e.home_assistant.info,r=Y(t),i=J(p.link);return A`<section class="card" data-testid="card-connection">
      <header class="card-head">
        ${B(`home-assistant`)}
        <h3>Home Assistant</h3>
        ${G(r.tone,r.label)}
      </header>
      ${t.state!==`connected`&&t.last_error?A`<p class="notice notice-${r.tone===`error`?`error`:`warning`}">${t.last_error}</p>`:M}
      ${K([[`Home`,n?.location_name],[`Version`,n?.version??t.ha_version],[`Connected since`,X(t.connected_since)],[`Response time`,t.latency_ms==null?null:`${t.latency_ms} ms`],[`Entities / areas`,n?`${n.entity_count??`—`} / ${n.area_count??`—`}`:null],[`State changes`,t.connected?`${t.events_per_minute??0} in the last minute`:null],[`Live updates`,i.label]])}
    </section>`}renderApp(e){let t=tt[e.app.health]??tt.warning,n=e.checks.filter(e=>e.status===`error`||e.status===`warning`);return A`<section class="card" data-testid="card-app">
      <header class="card-head">
        ${B(`lightbulb-auto`)}
        <h3>App</h3>
        ${G(t.tone,t.label)}
      </header>
      ${K([[`Version`,e.app.version],[`Phase`,`${e.app.phase} · foundation (read-only)`],[`Running for`,Qe(e.app.uptime_s)],[`Health checks`,n.length?et(n.length,`needs attention`,`need attention`):`All passed`]])}
      ${n.length?A`<ul class="plain-list">
            ${n.map(e=>A`<li>${G(e.status,e.title)} <span class="muted">${e.detail}</span></li>`)}
          </ul>`:M}
      <a class="text-link" href="#/diagnostics" @click=${e=>(e.preventDefault(),U(`diagnostics`))}
        >Open Diagnostics ${B(`chevron-right`,18)}</a
      >
    </section>`}renderIntegration(e){let t=e.integration,n=Je(t),r=t?.entries[0],i=Object.entries(t?.entities_by_domain??{}).map(([e,t])=>`${t} ${e.replace(`_`,` `)}`).join(`, `);return A`<section class="card" data-testid="card-integration">
      <header class="card-head">
        ${B(`puzzle`)}
        <h3>Existing integration</h3>
        ${G(n.tone,n.label)}
      </header>
      <p class="muted">
        Smart Occupancy & Lighting custom integration (<code>${t?.domain??`smart_occupancy`}</code>).
        ${t?.detected?`It keeps running all automation. This App only reads its status and never changes it.`:`When it is installed, the App shows its status here. Nothing is installed or changed automatically.`}
      </p>
      ${t?.detected?K([[`Version`,t.version],[`Configuration`,r?`${r.title??`—`} (${r.state??`unknown`})`:`Not set up`],[`Entities`,t.entity_count?`${t.entity_count} (${i})`:`None`]]):M}
      ${e.integration_entities.length?A`<details class="entities">
            <summary>Live entity states (${e.integration_entities.length})</summary>
            <ul class="entity-list">
              ${e.integration_entities.map(e=>A`<li data-entity=${e.entity_id}>
                  <span class="entity-main">
                    <span class="entity-name">${e.name}</span>
                    <span class="entity-id">${e.entity_id}</span>
                  </span>
                  <span class="entity-state">
                    <span class="state">${$e(e.state,e.unit)}</span>
                    <span class="muted small">${X(e.last_changed)}</span>
                  </span>
                </li>`)}
            </ul>
          </details>`:M}
    </section>`}renderModules(e){return A`<section class="card wide" data-testid="card-modules">
      <header class="card-head">
        ${B(`view-dashboard`)}
        <h3>Modules</h3>
      </header>
      <div class="tiles">
        ${e.modules.map(e=>A`<a
            class="tile"
            href="#/${e.key}"
            data-module=${e.key}
            @click=${t=>(t.preventDefault(),U(e.key))}
          >
            <span class="tile-icon">${B(e.icon)}</span>
            <span class="tile-body">
              <span class="tile-title">${e.title}</span>
              ${e.status===`available`?G(`ok`,`Available`):G(`pending`,`Pending`)}
            </span>
          </a>`)}
      </div>
    </section>`}renderMigration(e){let t=e.migration;return A`<section class="card wide" data-testid="card-migration">
      <header class="card-head">
        ${B(`pending`)}
        <h3>Migration progress</h3>
        ${G(t.modules_migrated===t.modules_total?`ok`:`pending`,`${t.modules_migrated} of ${t.modules_total} modules`)}
      </header>
      <div
        class="progress"
        role="progressbar"
        aria-label="Modules migrated"
        aria-valuemin="0"
        aria-valuemax=${t.modules_total}
        aria-valuenow=${t.modules_migrated}
      >
        <span style="width: ${t.percent}%"></span>
      </div>
      <p class="muted">
        The App foundation (Phase ${t.current_phase}) is in place. Modules move one at a time, only after you approve
        it, and the matching integration function is switched off only after the replacement is validated. Each
        module is controlled by exactly one system: today the integration controls ${t.controlled_by_integration} of
        ${t.modules_total}, the App ${t.controlled_by_app}.
      </p>
      <ol class="steps">
        ${t.items.map(e=>A`<li class="step step-${e.status}">
            <span class="step-mark" aria-hidden="true">${B(e.status===`done`?`ok`:`pending`,18)}</span>
            <span class="step-title"
              >${e.title}
              ${e.controlled_by?A`<span class="muted small block">In control: ${e.controlled_by===`app`?`App`:`integration`}</span>`:M}</span
            >
            ${e.status===`done`?G(`ok`,`Done`):G(`pending`,`Pending`)}
          </li>`)}
      </ol>
    </section>`}render(){let e=p.overview;return A`
      ${q(`Dashboard`,`Status of the App, its Home Assistant connection and the migration.`)}
      <p class="notice notice-info" data-testid="readonly-notice">
        ${B(`shield-check`,20)}
        <span
          >Phase A1 is read-only. The App reads Home Assistant but does not control devices or change any
          configuration. Your existing Smart Occupancy setup keeps working unchanged.</span
        >
      </p>
      ${e?A`<div class="grid">
            ${this.renderConnection(e)} ${this.renderApp(e)} ${this.renderIntegration(e)} ${this.renderModules(e)}
            ${this.renderMigration(e)}
          </div>`:Ye()}
    `}};customElements.define(`sl-dashboard`,nt);var rt=6e4,Z=null,it=null;async function at(e=!1){return!e&&Z&&Date.now()-Z.at<rt?Z.data:(it??=f(`api/lookup`).then(e=>(Z={data:e,at:Date.now()},e)).finally(()=>{it=null}),it)}function Q(e){return e.normalize(`NFKD`).replace(/\p{M}/gu,``).replace(/ð/g,`d`).replace(/Ð/g,`d`).replace(/[øØ]/g,`o`).replace(/[æÆ]/g,`ae`).toLowerCase()}function ot(e,t){let n=(...e)=>e.filter(Boolean).join(` · `);switch(t){case`area`:return e.areas.map(e=>({id:e.id,primary:e.name,secondary:`${e.device_count} devices · ${e.entity_count} entities`,search:Q(`${e.name} ${e.id}`)}));case`device`:return e.devices.map(e=>({id:e.id,primary:e.name,secondary:n(e.area,e.manufacturer,e.model),search:Q(n(e.name,e.area,e.manufacturer,e.model))}));default:return e.entities.map(e=>({id:e.entity_id,primary:e.name,secondary:n(e.entity_id,e.area),search:Q(n(e.name,e.entity_id,e.area,e.device))}))}}function st(e,t,n){let r=Q(t).split(/\s+/).filter(Boolean);if(!r.length)return{items:e.slice(0,n),total:e.length};let i=e.filter(e=>r.every(t=>e.search.includes(t))),a=r[0];return i.sort((e,t)=>+!Q(e.primary).startsWith(a)-!Q(t.primary).startsWith(a)||e.primary.localeCompare(t.primary)),{items:i.slice(0,n),total:i.length}}var ct={entity:{title:`Choose an entity`,placeholder:`Choose an entity…`,search:`Search name, entity ID or area`},device:{title:`Choose a device`,placeholder:`Choose a device…`,search:`Search name, area or model`},area:{title:`Choose an area`,placeholder:`Choose an area…`,search:`Search areas`}},lt=60,ut=0,dt=class extends W{static{this.properties={kind:{type:String},value:{type:String},label:{type:String},open:{state:!0},query:{state:!0},active:{state:!0},all:{state:!0},error:{state:!0}}}constructor(){super(),this.uid=`picker-${++ut}`,this.loadedKind=null,this.kind=`entity`,this.value=null,this.label=``,this.open=!1,this.query=``,this.active=0,this.all=null,this.error=null}get dialog(){return this.querySelector(`dialog`)}async load(e=!1){try{let t=await at(e);this.all=ot(t,this.kind),this.loadedKind=this.kind,this.error=null}catch(e){this.error=`Could not load the list (${String(e)}).`}}willUpdate(e){e.has(`kind`)&&this.loadedKind&&this.loadedKind!==this.kind&&(this.all=null,this.load())}async show(){this.query=``,this.active=0,this.open=!0,await this.updateComplete,this.dialog?.showModal(),this.querySelector(`.picker-search input`)?.focus(),await this.load(!0)}close(){this.dialog?.close()}onClosed(){this.open=!1,this.querySelector(`.picker-field`)?.focus()}choose(e){this.value=e.id,this.dispatchEvent(new CustomEvent(`value-changed`,{detail:{kind:this.kind,value:e.id},bubbles:!0,composed:!0})),this.close()}onInput(e){this.query=e.target.value,this.active=0}onKey(e,t){if(e.key===`ArrowDown`||e.key===`ArrowUp`){e.preventDefault();let n=e.key===`ArrowDown`?1:-1;this.active=Math.max(0,Math.min(t.length-1,this.active+n)),this.querySelector(`#${this.uid}-opt-${this.active}`)?.scrollIntoView({block:`nearest`})}else e.key===`Enter`&&t[this.active]&&(e.preventDefault(),this.choose(t[this.active]))}selectedLabel(){if(!this.value)return null;let e=this.all?.find(e=>e.id===this.value);return e?e.primary:this.value}render(){let e=ct[this.kind]??ct.entity,t=this.all?st(this.all,this.query,lt):{items:[],total:0},n=t.items,r=this.selectedLabel(),i=`${this.uid}-list`;return A`
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
        ${B(`chevron-right`,20)}
      </button>
      ${this.open?A`<dialog class="sheet sheet-full" aria-labelledby="${this.uid}-title" @close=${this.onClosed}
            @click=${e=>e.target===this.dialog&&this.close()}>
            <div class="sheet-inner">
              <header class="sheet-head">
                <h2 id="${this.uid}-title">${e.title}</h2>
                <button type="button" class="icon-btn" aria-label="Close" @click=${this.close}>${B(`close`)}</button>
              </header>
              <div class="picker-search">
                ${B(`search`,20)}
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
                  aria-activedescendant=${n.length?`${this.uid}-opt-${this.active}`:M}
                  aria-label=${e.search}
                  placeholder=${e.search}
                  .value=${this.query}
                  @input=${this.onInput}
                  @keydown=${e=>this.onKey(e,n)}
                />
              </div>
              <div class="picker-results">
                ${this.error?A`<p class="notice notice-error">${this.error}</p>`:this.all?n.length?A`<ul id=${i} role="listbox" aria-label=${e.title}>
                          ${n.map((e,t)=>A`<li
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
                        ${t.total>n.length?A`<p class="muted small pad">
                              Showing ${n.length} of ${t.total}. Type more to narrow the list.
                            </p>`:M}`:A`<p class="muted pad">Nothing matches “${this.query}”.</p>`:A`<p class="muted pad">Loading…</p>`}
              </div>
            </div>
          </dialog>`:M}
    `}};customElements.define(`sl-picker`,dt);var ft=[{value:`entity`,label:`Entity`},{value:`device`,label:`Device`},{value:`area`,label:`Area`}],pt=class extends W{static{this.properties={kind:{state:!0},selected:{state:!0},data:{state:!0}}}constructor(){super(),this.kind=`entity`,this.selected=null,this.data=null}setKind(e){this.kind=e,this.selected=null}async onPicked(e){this.selected=e.detail.value,this.data=await at()}renderDetails(){let e=this.data;if(!e||!this.selected)return M;if(this.kind===`entity`){let t=e.entities.find(e=>e.entity_id===this.selected);return t?K([[`Name`,t.name],[`Entity ID`,A`<code>${t.entity_id}</code>`],[`State`,$e(t.state,t.unit)],[`Last changed`,X(t.last_changed)],[`Area`,t.area],[`Device`,t.device],[`Integration`,t.platform]]):M}if(this.kind===`device`){let t=e.devices.find(e=>e.id===this.selected);if(!t)return M;let n=e.entities.filter(e=>e.device_id===t.id);return A`${K([[`Name`,t.name],[`Area`,t.area],[`Manufacturer`,t.manufacturer],[`Model`,t.model],[`Entities`,String(n.length)]])}
        ${n.length?A`<ul class="plain-list">
              ${n.slice(0,30).map(e=>A`<li>${e.name} <span class="muted">· ${$e(e.state,e.unit)}</span></li>`)}
            </ul>`:M}`}let t=e.areas.find(e=>e.id===this.selected);return t?K([[`Name`,t.name],[`Area ID`,A`<code>${t.id}</code>`],[`Devices`,String(t.device_count)],[`Entities`,String(t.entity_count)]]):M}render(){return A`<section class="card wide" data-testid="card-lookup">
      <header class="card-head">${B(`search`)}<h3>Home Assistant lookup</h3></header>
      <p class="muted">Find an entity, device or area and see how Home Assistant reports it. Read-only.</p>
      <fieldset class="segmented" aria-label="Look up">
        <legend class="visually-hidden">Look up</legend>
        ${ft.map(e=>A`<label class=${this.kind===e.value?`selected`:``}>
            <input type="radio" name="lookup-kind" value=${e.value} .checked=${this.kind===e.value} @change=${()=>this.setKind(e.value)} />
            ${e.label}
          </label>`)}
      </fieldset>
      <div class="lookup-body">
        <sl-picker .kind=${this.kind} .value=${this.selected} label="Find" @value-changed=${this.onPicked}></sl-picker>
        <div class="lookup-details" data-testid="lookup-details">
          ${this.selected?this.renderDetails():A`<p class="muted">Choose an item to see its details.</p>`}
        </div>
      </div>
    </section>`}};customElements.define(`sl-lookup-card`,pt);function $(e){return e==null||e===``?`—`:typeof e==`boolean`?e?`Yes`:`No`:String(e)}var mt=class extends W{static{this.properties={details:{state:!0},loadingDetails:{state:!0},detailsError:{state:!0},showJson:{state:!0},copied:{state:!0}}}constructor(){super(),this.details=null,this.loadingDetails=!1,this.detailsError=null,this.showJson=!1,this.copied=null}async copyJson(){let e=JSON.stringify(this.details,null,2);try{await navigator.clipboard.writeText(e),this.copied=`Copied`}catch{let e=this.querySelector(`textarea.json-view`);e?.focus(),e?.select(),this.copied=`Selected: use your device's Copy`}window.setTimeout(()=>this.copied=null,4e3)}connectedCallback(){super.connectedCallback(),this.loadDetails()}async loadDetails(){this.loadingDetails=!0;try{this.details=await f(`api/diagnostics`),this.detailsError=null}catch(e){this.detailsError=String(e)}finally{this.loadingDetails=!1}}render(){let e=p.overview,t=A`<div class="actions">
      <button class="btn" @click=${()=>void this.loadDetails()} ?disabled=${this.loadingDetails}>
        ${B(`refresh`,18)} Refresh
      </button>
      <button class="btn" @click=${()=>this.showJson=!this.showJson} aria-expanded=${this.showJson?`true`:`false`} data-testid="view-diagnostics">
        ${B(`json`,18)} ${this.showJson?`Hide`:`View`} data
      </button>
      <a class="btn btn-primary" href=${d(`api/diagnostics?download=1`)} download data-testid="download-diagnostics">
        ${B(`download`,18)} Download
      </a>
    </div>`;if(!e)return A`${q(`Diagnostics`,`Health checks and connection details.`,t)} ${Ye()}`;let n=e.home_assistant.connection,r=e.supervisor,i=this.details,a=J(p.link);return A`
      ${q(`Diagnostics`,`Health checks and connection details.`,t)}
      ${this.showJson&&i?A`<section class="card json-card" data-testid="card-json">
            <header class="card-head">${B(`json`)}<h3>Diagnostics data</h3>
              <button class="btn" @click=${()=>void this.copyJson()} data-testid="copy-diagnostics">${B(`copy`,18)} ${this.copied??`Copy`}</button>
            </header>
            <p class="muted small">Share this when reporting a problem. It contains no passwords or tokens.</p>
            <textarea class="json-view mono" readonly rows="14" aria-label="Diagnostics data">${JSON.stringify(i,null,2)}</textarea>
          </section>`:M}
      <div class="grid">
        <section class="card" data-testid="card-checks">
          <header class="card-head">${B(`stethoscope`)}<h3>Health checks</h3></header>
          <ul class="checks">
            ${e.checks.map(e=>A`<li data-check=${e.key} data-status=${e.status}>
                ${qe(e.status)}
                <span><strong>${e.title}</strong><br /><span class="muted">${e.detail}</span></span>
              </li>`)}
          </ul>
        </section>

        <section class="card" data-testid="card-safety">
          <header class="card-head">${B(`shield-check`)}<h3>Safety</h3>${G(`ok`,`Read-only`)}</header>
          ${K([[`Service calls sent`,String(e.safety.service_calls_sent)],[`Device control`,e.safety.device_control?`On`:`Off`],[`Automation engine`,e.safety.automation_engine?`Running`:`None (the integration stays in charge)`],[`Automatic migration`,e.safety.automatic_migration?`On`:`Off`],[`Changes to the integration`,e.safety.modifies_integration?`Yes`:`None`]])}
          <details>
            <summary>Home Assistant requests made</summary>
            <ul class="plain-list mono">
              ${Object.entries(e.safety.commands_sent).map(([e,t])=>A`<li>${e}: ${t}</li>`)}
            </ul>
            <p class="muted small">Allowed (all read-only): ${e.safety.allowed_commands.join(`, `)}.</p>
          </details>
        </section>

        <section class="card" data-testid="card-ha-connection">
          <header class="card-head">${B(`home-assistant`)}<h3>Home Assistant connection</h3>
            ${G(Y(n).tone,Y(n).label)}</header>
          ${K([[`State`,n.state],[`Version`,n.ha_version],[`Connected since`,X(n.connected_since)],[`Last disconnect`,X(n.last_disconnect)],[`Reconnects`,n.reconnects],[`Response time`,n.latency_ms==null?null:`${n.latency_ms} ms`],[`State changes received`,n.events_total],[`Last error`,n.last_error],[`Browser link`,a.label]])}
        </section>

        <section class="card" data-testid="card-app-details">
          <header class="card-head">${B(`lightbulb-auto`)}<h3>App</h3></header>
          ${K([[`Version`,e.app.version],[`Phase`,e.app.phase],[`Started`,X(e.app.started_at)],[`Running for`,Qe(e.app.uptime_s)],r?.available?[`Latest version`,$(r.app.version_latest)]:null,r?.available?[`Architecture`,$(r.app.arch)]:null,i?[`Python`,i.runtime.python]:null,i?[`aiohttp`,i.runtime.aiohttp]:null])}
        </section>

        <section class="card" data-testid="card-system">
          <header class="card-head">${B(`info`)}<h3>System</h3>
            ${r?.configured?G(r.available?`ok`:`warning`,r.available?`Supervisor`:`Unavailable`):G(`neutral`,`Development`)}</header>
          ${r?.available?K([[`Home Assistant`,$(r.system.homeassistant)],[`Supervisor`,$(r.system.supervisor)],[`Operating system`,$(r.system.operating_system)],[`Machine`,$(r.system.machine)],[`Update channel`,$(r.system.channel)],[`Supported / healthy`,`${$(r.system.supported)} / ${$(r.system.healthy)}`]]):A`<p class="muted">${r?.error??`Supervisor information is not available.`}</p>`}
        </section>

        <sl-lookup-card></sl-lookup-card>

        <section class="card wide" data-testid="card-log">
          <header class="card-head">${B(`stethoscope`)}<h3>Recent log</h3></header>
          ${this.detailsError?A`<p class="notice notice-error">Could not load details: ${this.detailsError}</p>`:M}
          <details>
            <summary>Show the last ${i?Math.min(i.log.length,100):0} log entries</summary>
            <div class="log mono">
              ${(i?.log??[]).slice(-100).reverse().map(e=>A`<div class="log-line log-${e.level}">
                  <span class="nowrap">${X(e.time)}</span> <span class="log-level">${e.level}</span>
                  <span>${e.message}</span>
                </div>`)}
            </div>
          </details>
        </section>
      </div>
    `}};customElements.define(`sl-diagnostics`,mt);var ht=class extends W{static{this.properties={moduleKey:{type:String}}}render(){let e=V.find(e=>e.key===this.moduleKey),t=p.overview?.modules.find(e=>e.key===this.moduleKey),n=t?.title??e?.title??this.moduleKey,r=p.overview?.integration,i=Je(r),a=r?.status===`loaded`;return A`
      ${q(n,t?.summary??``,G(`pending`,`Migration pending`))}
      <section class="card pending-card" data-testid="pending-${this.moduleKey}">
        <div class="pending-hero">
          <span class="pending-icon">${B(t?.icon??e?.icon??`puzzle`,40)}</span>
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
            ${t?.controlled_by===`app`?G(`ok`,`This App`):A`${G(`info`,`Smart Occupancy integration`)} ${G(i.tone,i.label)}`}
            ${a&&t?.controlled_by!==`app`?A`<span class="muted block">It keeps handling ${n.toLowerCase()} unchanged. Only one system
                  controls these devices at a time.</span>`:M}
          </dd>
        </dl>
      </section>
    `}};customElements.define(`sl-pending-module`,ht);var gt=[{value:`auto`,label:`Automatic`},{value:`light`,label:`Light`},{value:`dark`,label:`Dark`}],_t=class extends W{static{this.properties={theme:{state:!0},advanced:{state:!0},settings:{state:!0}}}constructor(){super(),this.theme=o(),this.advanced=l(),this.settings=null}connectedCallback(){super.connectedCallback(),this.advanced&&this.loadSettings()}async loadSettings(){try{this.settings=(await f(`api/diagnostics`)).settings}catch{this.settings=null}}chooseTheme(e){this.theme=e,s(e)}toggleAdvanced(e){this.advanced=e.target.checked,u(this.advanced),this.advanced&&!this.settings&&this.loadSettings()}renderAdvanced(){let e=p.overview,t=this.settings,n=new URL(document.baseURI).pathname;return A`<section class="card wide" data-testid="advanced-settings">
      <header class="card-head">${B(`cog`)}<h3>Advanced</h3></header>
      <p class="muted">
        Technical details, for troubleshooting. App options (such as the log level) are changed in Home Assistant:
        Settings → Apps → Smart Occupancy &amp; Lighting → Configuration.
      </p>
      ${K([[`Log level`,t?.log_level],[`Home Assistant API`,t?.home_assistant_websocket],[`Supervisor API`,t?.supervisor_api],[`API credentials`,t?.token],[`Accepted clients`,Array.isArray(t?.allowed_clients)?(t?.allowed_clients).join(`, `):null],[`Interface path`,n],[`Live updates`,J(p.link).label],[`Development mode`,e?.app.dev_mode?`On`:`Off`]])}
    </section>`}render(){let e=p.overview;return A`
      ${q(`Settings`,`Appearance and information about the App.`)}
      <div class="grid grid-2">
        <section class="card" data-testid="appearance">
          <header class="card-head">${B(`palette`)}<h3>Appearance</h3></header>
          <fieldset class="segmented" aria-label="Theme">
            <legend>Theme</legend>
            ${gt.map(e=>A`<label class=${this.theme===e.value?`selected`:``}>
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
          <header class="card-head">${B(`lightbulb-auto`)}<h3>About</h3></header>
          ${K([[`App`,e?.app.name??`Smart Occupancy & Lighting`],[`Version`,e?.app.version],[`Phase`,e?`${e.app.phase} (foundation, read-only)`:null],[`Feature settings`,`Not in the App yet: they are migrated module by module`]])}
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
        ${this.advanced?this.renderAdvanced():M}
      </div>
    `}};customElements.define(`sl-settings`,_t);var vt=class extends W{static{this.properties={route:{state:!0},railExpanded:{state:!0},moreOpen:{state:!0}}}constructor(){super(),this.onHashChange=()=>{let e=H();e!==this.route&&(this.route=e,window.scrollTo({top:0}),this.updateComplete.then(()=>this.querySelector(`.page-header h2`)?.focus({preventScroll:!0}))),this.railExpanded=!1,this.closeMore()},this.onKey=e=>{e.key===`Escape`&&this.railExpanded&&(this.railExpanded=!1)},this.route=H(),this.railExpanded=!1,this.moreOpen=!1}connectedCallback(){super.connectedCallback(),window.addEventListener(`hashchange`,this.onHashChange),window.addEventListener(`keydown`,this.onKey)}disconnectedCallback(){super.disconnectedCallback(),window.removeEventListener(`hashchange`,this.onHashChange),window.removeEventListener(`keydown`,this.onKey)}onNav(e,t){e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||(e.preventDefault(),U(t),this.railExpanded=!1,this.closeMore())}async openMore(){this.moreOpen=!0,await this.updateComplete,this.querySelector(`dialog.more-sheet`)?.showModal()}closeMore(){let e=this.querySelector(`dialog.more-sheet`);e?.open&&e.close(),this.moreOpen=!1}isPending(e){return!Ge.has(e)&&(p.overview?.modules.find(t=>t.key===e)?.status??`pending`)===`pending`}navLink(e,t){let n=e.key===this.route,r=this.isPending(e.key);return A`<a
      href="#/${e.key}"
      class="nav-link ${n?`active`:``}"
      aria-current=${n?`page`:M}
      data-nav=${e.key}
      @click=${t=>this.onNav(t,e.key)}
    >
      ${B(e.icon)}
      <span class="label">${e.title}</span>
      ${r?t===`sheet`?G(`pending`,`Pending`):A`<span class="pending-dot" aria-hidden="true"></span><span class="visually-hidden">(migration pending)</span>`:M}
    </a>`}renderBottomNav(){let e=!Ke.includes(this.route);return A`<nav class="bottom-nav" aria-label="Main">
      ${V.filter(e=>Ke.includes(e.key)).map(e=>{let t=e.key===this.route;return A`<a
          href="#/${e.key}"
          class=${t?`active`:``}
          aria-current=${t?`page`:M}
          data-bottom=${e.key}
          @click=${t=>this.onNav(t,e.key)}
          >${B(e.icon)}<span>${e.title}</span></a
        >`})}
      <button
        type="button"
        class=${e?`active`:``}
        aria-haspopup="dialog"
        data-bottom="more"
        @click=${()=>void this.openMore()}
      >
        ${B(`more`)}<span>More</span>
      </button>
    </nav>`}renderMoreSheet(){return this.moreOpen?A`<dialog
      class="sheet sheet-bottom more-sheet"
      aria-labelledby="more-title"
      @close=${()=>this.moreOpen=!1}
      @click=${e=>e.target.matches(`dialog`)&&this.closeMore()}
    >
      <div class="sheet-inner">
        <header class="sheet-head">
          <h2 id="more-title">All modules</h2>
          <button type="button" class="icon-btn" aria-label="Close" @click=${()=>this.closeMore()}>${B(`close`)}</button>
        </header>
        <nav class="sheet-nav" aria-label="All modules">${V.map(e=>this.navLink(e,`sheet`))}</nav>
      </div>
    </dialog>`:M}renderPage(){switch(this.route){case`dashboard`:return A`<sl-dashboard></sl-dashboard>`;case`diagnostics`:return A`<sl-diagnostics></sl-diagnostics>`;case`settings`:return A`<sl-settings></sl-settings>`;default:return A`<sl-pending-module .moduleKey=${this.route}></sl-pending-module>`}}render(){let e=p.overview,t=V.find(e=>e.key===this.route)??V[0],n=J(p.link),r=p.link===`offline`,i=r?n:Y(e?.home_assistant.connection);return A`
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
              ${B(this.railExpanded?`menu-open`:`menu`)}
            </button>
            <span class="brand-mark">${B(`lightbulb-auto`)}</span>
            <span class="brand-text">Smart Lighting</span>
          </div>
          <nav class="nav" aria-label="Modules">${V.map(e=>this.navLink(e,`side`))}</nav>
          <div class="sidebar-foot">
            <span class="muted small">Phase ${e?.app.phase??`A1`} · v${e?.app.version??`…`}</span>
          </div>
        </aside>
        <div class="scrim" @click=${()=>this.railExpanded=!1}></div>
        <div class="main">
          <header class="topbar">
            <span class="topbar-mark">${B(`lightbulb-auto`,22)}</span>
            <h1 class="page-title">${t.title}</h1>
            <span class="topbar-status" data-testid="connection-pill" title=${`App link: ${n.label}`}>
              ${G(i.tone,i.label)}
              <span class="visually-hidden">${r?``:`Home Assistant connection`}</span>
            </span>
          </header>
          <main class="content" id="content" tabindex="-1">${this.renderPage()}</main>
        </div>
        ${this.renderBottomNav()}
      </div>
      ${this.renderMoreSheet()}
    `}};customElements.define(`sl-app`,vt),c(),new ne().start();