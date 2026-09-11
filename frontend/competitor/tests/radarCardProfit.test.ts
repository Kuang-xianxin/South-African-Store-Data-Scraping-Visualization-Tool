import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { compileScript, parse } from "@vue/compiler-sfc";
import ts from "typescript";
import * as vue from "vue";
import * as helpers from "../src/radarCardProfit.ts";
import * as sellerHelpers from "../src/radarSellerProducts.ts";

test("platform titles use only complete matching HTTPS Takealot URLs", () => {
  const valid = "https://www.takealot.com/real-saved-product-name/PLID100149090";
  assert.equal(helpers.radarCardPlatformUrl({ plid: "100149090", 链接: valid }), valid);
  for (const value of [null, "https://www.takealot.com/PLID100149090", valid.replace("100149090", "2"), valid.replace("https:", "http:"), valid.replace("www.takealot.com", "www.takealot.com.evil.test"), valid.replace("www.takealot.com", "user:password@www.takealot.com"), "javascript:alert(1)"]) {
    assert.equal(helpers.radarCardPlatformUrl({ plid: "100149090", 链接: value }), null);
  }
});
const row = (profit: number, margin: number, extra = {}) => ({ plid: "100149090", selling_price_zar: 514, workbook_profit: { status: "available", calculation: { profit_zar: profit, margin_percentage: margin } }, ...extra } as any);
test("profit ranges retain losses, known zero, partial evidence and PLID boundaries", () => {
  const result = helpers.radarCardProfitSummary([row(-15,-3),row(0,0),row(51,10),row(800,90,{plid:"999"}),row(0,0,{workbook_profit:{status:"unavailable",message:"成本缺失",calculation:null}})],"100149090");
  assert.deepEqual(result.profit,[-15,51]); assert.deepEqual(result.margin,[-3,10]);
  assert.equal(result.total,4); assert.equal(result.available,3); assert.deepEqual(result.reasons,["成本缺失"]);
  assert.deepEqual(helpers.radarCardProfitSummary([row(0,0)],"100149090").profit,[0,0]);
});
test("missing workbook evidence and non-finite values never fall back to gross profit", () => {
  const result=helpers.radarCardProfitSummary([row(NaN,8),row(0,0,{workbook_profit:undefined,scenarios:{current_gross:{profit_rmb:999}}})],"100149090");
  assert.equal(result.profit,null); assert.equal(result.available,0);
});

function component(name: string, modules: Record<string,unknown>) {
  // The host renderer has no DOM body; retain the dialog inside the test root.
  const source=readFileSync(new URL(`../src/components/${name}.vue`,import.meta.url),"utf8")
    .replace(/<Teleport to="body">/g,"<div>").replace(/<\/Teleport>/g,"</div>");
  const compiled=compileScript(parse(source).descriptor,{id:"radar-card-test",inlineTemplate:true}).content;
  const code=ts.transpileModule(compiled,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
  const exports:any={};
  runInNewContext(code,{exports,Intl,AbortController,URL,Error,window:modules.__window,require:(module:string)=>{
    if(module==="vue")return modules.__vue ?? vue;
    if(module==="../radarCardProfit")return helpers;
    if(module==="../radarSellerProducts")return sellerHelpers;
    if(module in modules)return modules[module];
    if(module.endsWith(".vue"))return {default:vue.defineComponent({render:()=>vue.h("div")})};
    throw Error(module);
  }});
  return exports.default;
}
type Node = {tag:string;children:Node[];props:Record<string,any>;text:string;parent?:Node};
const node=(tag:string,text=""):Node=>Object.assign({tag,text,props:{},children:[],addEventListener(){},removeEventListener(){}},tag==="dialog"?{showModal(){},close(){}}:{});
const renderer=vue.createRenderer<Node,Node>({
  createElement:(tag)=>node(tag),createText:(text)=>node("#text",text),createComment:(text)=>node("#comment",text),
  setText:(n,text)=>{n.text=text},setElementText:(n,text)=>{n.text=text;n.children=[]},
  parentNode:(n)=>n.parent??null,nextSibling:(n)=>n.parent?.children[n.parent.children.indexOf(n)+1]??null,
  insert:(n,parent,anchor)=>{if(n.parent)n.parent.children.splice(n.parent.children.indexOf(n),1);n.parent=parent;const index=anchor?parent.children.indexOf(anchor):-1;index<0?parent.children.push(n):parent.children.splice(index,0,n)},
  remove:(n)=>{if(n.parent)n.parent.children.splice(n.parent.children.indexOf(n),1)},patchProp:(n,key,_old,value)=>{n.props[key]=value},
});
function walk(n:Node):Node[]{return [n,...n.children.flatMap(walk)]}
const text=(n:Node):string=>n.text+n.children.map(text).join("");
const item:any={来源:"own_store",plid:"100149090",商品:"Sample title",链接:"https://www.takealot.com/sample-title/PLID100149090",自有报价:[],跟卖报价:[],对比报价:[],类目路径:[],采集时间:"2026-09-11",company_skus:[],评论数:0,周期销售额:null};

test("card body and title do not open detail; explicit sibling actions dispatch separately", () => {
  const Card=component("CompetitorRadarProductCard",{
    "../numberFormatters":{cachedNumberFormatter:(locale:string,options:any)=>new Intl.NumberFormat(locale,options)},
    "../ownOfferLatestStatus":{ownOfferLatestStatusLabel:(value:string)=>value},
    "../competitorOfferHistory":{comparisonOffers:()=>[],followerOffers:()=>[],groupCompetitorOffersBySeller:()=>[]},
    "../time":{formatChinaDateTime:(value:string)=>value??"—"},
  });
  const root=node("root");const actions:string[]=[];
  const app=renderer.createApp(Card,{item,onOpenDetail:()=>actions.push("detail"),onQueryCompetitors:()=>actions.push("query")});app.mount(root);
  const all=walk(root),article=all.find((n)=>n.tag==="article")!;
  assert.equal(article.props.onClick,undefined);assert.equal(article.props.onKeydown,undefined);assert.equal(article.props.role,undefined);assert.equal(article.props.tabindex,undefined);
  const title=all.find((n)=>n.tag==="a")!;assert.equal(title.props.href,item.链接);assert.equal(title.props.target,"_blank");assert.equal(title.props.onClick,undefined);
  const detail=all.find((n)=>n.tag==="button"&&text(n)==="查看详情")!;
  const query=all.find((n)=>n.tag==="button"&&text(n).trim()==="竞品查询")!;
  assert.equal(detail.parent,query.parent);
  detail.props.onClick();query.props.onClick({stopPropagation(){}});assert.deepEqual(actions,["detail","query"]);app.unmount();
});

test("profit displays immediately from list data without requests or expansion",()=>{
  const Profit=component("RadarCardProfit",{});
  const summary={profit:[-15,62],margin:[-3,12],available:2,total:3,reasons:["成本缺失"]};
  const root=node("root");const app=renderer.createApp(Profit,{item:{...item,own_profit_summary:summary},storeScope:"all"});app.mount(root);
  assert.match(text(root),/62[,.]00/);assert.match(text(root),/12%/);assert.match(text(root),/2\/3 报价可算/);
  assert.match(walk(root).find(n=>n.tag==="section")!.props.title,/成本缺失/);
  assert.equal(walk(root).some(n=>["details","button"].includes(n.tag)),false);app.unmount();
});

test("profit follows replacement card data and clears unavailable results",async()=>{
  const Profit=component("RadarCardProfit",{});
  const selected=vue.ref({...item,own_profit_summary:{profit:[62,62],margin:[12,12],available:1,total:1,reasons:[]}});
  const root=node("root");const app=renderer.createApp({setup:()=>()=>vue.h(Profit,{item:selected.value,storeScope:"all"})});app.mount(root);
  assert.match(text(root),/62[,.]00/);
  selected.value={...item,own_profit_summary:undefined};await vue.nextTick();
  assert.doesNotMatch(text(root),/62[,.]00/);assert.match(text(root),/待核算/);
  selected.value={...item,own_profit_summary:{profit:[0,0],margin:[0,0],available:1,total:1,reasons:[]}};await vue.nextTick();
  assert.match(text(root),/0[,.]00/);assert.match(text(root),/利润率 0%/);app.unmount();
});

test("seller matching keeps exact IDs and private store identities, and brands are not guessed",()=>{
  const source:any={...item,对比报价:[{卖家ID:"M12",卖家:"Same Name"},{卖家ID:"123",卖家:"Same Name"}],自有报价:[{店铺:"Our Shop",store_code:"store-03"}]};
  const sellers=sellerHelpers.radarCardSellers(source);assert.equal(sellers.length,3);
  assert.equal(sellerHelpers.matchesRadarSeller({...item,对比报价:[{卖家ID:"123",卖家:"Same Name"}]} as any,sellers[0]),false);
  assert.equal(sellerHelpers.matchesRadarSeller({...item,自有报价:[{店铺:"Our Shop",store_code:"store-04"}]} as any,sellers[2]),false);
  assert.equal(sellerHelpers.radarBrandLabel({品牌:"  Real Brand  "}),"Real Brand");
  assert.equal(sellerHelpers.radarBrandLabel({}),"待采集");
  const actualShape:any={...item,对比报价:[{卖家ID:"store-03",卖家:"YeboShop",报价来源:"seller_api"}],自有报价:[{店铺:"YeboShop"}]};
  const ownSeller=sellerHelpers.radarCardSellers(actualShape);
  assert.deepEqual(ownSeller,[{key:"store:store-03",storeCode:"store-03",id:null,name:"YeboShop"}]);
  assert.equal(sellerHelpers.matchesRadarSeller({...actualShape,对比报价:[{卖家ID:"store-04",卖家:"YeboShop",报价来源:"seller_api"}]} as any,ownSeller[0]),false);
});

test("vertical official and observed sales retain every period, unknown values and incomplete totals",()=>{
  const Own=component("OwnStoreSalesComparisonMetrics",{});
  const root=node("root");const app=renderer.createApp(Own,{ownValues:{7:0,15:15,30:30,60:60,90:90,total:101,total_missing_days:2},followerValues:{7:7,30:3}});app.mount(root);
  const all=walk(root);assert.equal(all.filter(n=>n.tag==="tr").length,7);
  for(const heading of ["7天","15天","30天","60天","90天","累计","自有官方","跟卖观察"])assert.match(text(root),new RegExp(heading));
  assert.equal(all.filter(n=>n.tag==="td")[0].text,"0");assert.match(text(root),/未完整/);assert.match(text(root),/数据不足/);app.unmount();
  const Observed=component("CompetitorObservedSalesMetrics",{});const second=node("root");const view=renderer.createApp(Observed,{compact:true,values:{7:0,15:1,30:2,60:3,90:4,total:9}});view.mount(second);
  assert.equal(walk(second).filter(n=>n.tag==="tr").length,7);assert.equal(walk(second).filter(n=>n.tag==="td").length,6);assert.match(walk(second).find(n=>n.props.class==="sales-total-row")!.props.title,/不等同平台总订单/);view.unmount();
});

test("seller dialog reads beyond one server page, rejects lookalike IDs and deduplicates PLIDs",async()=>{
  const calls:number[]=[];
  const seller={key:"id:12",id:"12",storeCode:null,name:"Shop"};
  const target=(plid:string,id="12")=>({...item,plid,来源:"competitor",图片:null,价格:10,库存上限:"2",对比报价:[{卖家ID:id,卖家:"Shop"}]});
  const Modal=component("RadarSellerProductsModal",{
    __window:{addEventListener(){},removeEventListener(){}},
    __vue:{...vue,vModelText:{}},
    "../productImages":{productThumbnailUrl:()=>""},
    "../api":{AUTH_SESSION_ENDING_EVENT:"end",fetchCompetitors:async(...args:any[])=>{calls.push(args[6].page);return{items:args[6].page===1?[target("1"),target("2","123")]:[target("3"),target("1")],pagination:{total:101}}},fetchOwnStoreCompetitors:async()=>({store_items:[],pagination:{total:0}})},
  });
  const root=node("root");const app=renderer.createApp(Modal,{seller,storeScope:"all"});app.mount(root);
  for(let tick=0;tick<5;tick++){await new Promise(setImmediate);await vue.nextTick()}
  assert.deepEqual(calls,[1,2]);assert.match(text(root),/共 2 个商品/);assert.doesNotMatch(text(root),/PLID2 ·/);app.unmount();
});

test("image preview opens on hover, fits the viewport, and closes on scroll or source change",async()=>{
  const listeners=new Map<string,Function>();
  const browser={innerWidth:800,innerHeight:500,addEventListener:(name:string,fn:Function)=>listeners.set(name,fn),removeEventListener:(name:string)=>listeners.delete(name)};
  const Image=component("RadarProductImage",{__window:browser});
  const source=vue.ref("/img/one.jpg");const root=node("root");
  const app=renderer.createApp({setup:()=>()=>vue.h(Image,{src:source.value,title:"Product",show:true})});app.mount(root);
  const trigger=walk(root).find(n=>n.tag==="button")!;
  Object.assign(trigger,{getBoundingClientRect:()=>({left:740,right:784,top:450})});
  await trigger.props.onMouseenter();await vue.nextTick();
  const preview=walk(root).find(n=>n.props.class==="radar-image-preview")!;
  assert.equal(trigger.props["aria-expanded"],true);assert.equal(preview.props.style.left,"452px");assert.equal(preview.props.style.top,"208px");
  listeners.get("scroll")!();await vue.nextTick();assert.equal(trigger.props["aria-expanded"],false);
  await trigger.props.onFocus();source.value="/img/two.jpg";await vue.nextTick();assert.equal(trigger.props["aria-expanded"],false);
  app.unmount();assert.equal(listeners.size,0);
});

test("closing seller dialog cancels pagination and rejects late results",async()=>{
  let resolve!:(value:any)=>void;let signal:AbortSignal|undefined;let ownCalls=0,closed=0;
  const Modal=component("RadarSellerProductsModal",{
    __window:{addEventListener(){},removeEventListener(){}},__vue:{...vue,vModelText:{}},
    "../productImages":{productThumbnailUrl:()=>""},
    "../api":{AUTH_SESSION_ENDING_EVENT:"end",fetchCompetitors:(_a:any,_b:any,_c:any,s:AbortSignal)=>{signal=s;return new Promise(r=>{resolve=r})},fetchOwnStoreCompetitors:async()=>{ownCalls++;return{store_items:[]}}},
  });
  const root=node("root");const app=renderer.createApp(Modal,{seller:{key:"id:12",id:"12",storeCode:null,name:"Shop"},storeScope:"all",onClose:()=>closed++});app.mount(root);
  walk(root).find(n=>n.tag==="button"&&text(n)==="关闭")!.props.onClick();assert.equal(closed,1);assert.equal(signal!.aborted,true);
  resolve({items:[{...item,商品:"Late product",对比报价:[{卖家ID:"12",卖家:"Shop"}]}],pagination:{total:101}});
  await new Promise(setImmediate);await vue.nextTick();assert.doesNotMatch(text(root),/Late product/);assert.equal(ownCalls,0);app.unmount();
});
