import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { compileScript, parse } from "@vue/compiler-sfc";
import ts from "typescript";
import * as vue from "vue";
import * as helpers from "../src/radarCardProfit.ts";

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
  const source=readFileSync(new URL(`../src/components/${name}.vue`,import.meta.url),"utf8");
  const compiled=compileScript(parse(source).descriptor,{id:"radar-card-test",inlineTemplate:true}).content;
  const code=ts.transpileModule(compiled,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
  const exports:any={};
  runInNewContext(code,{exports,Intl,AbortController,URL,Error,require:(module:string)=>{
    if(module==="vue")return vue;
    if(module==="../radarCardProfit")return helpers;
    if(module in modules)return modules[module];
    if(module.endsWith(".vue"))return {default:vue.defineComponent({render:()=>vue.h("div")})};
    throw Error(module);
  }});
  return exports.default;
}
type Node = {tag:string;children:Node[];props:Record<string,any>;text:string;parent?:Node};
const node=(tag:string,text=""):Node=>({tag,text,props:{},children:[]});
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

test("profit does not load on mount, deduplicates clicks, then renders the model result",async()=>{
  let requests=0;let resolve!:(data:any)=>void;
  const Profit=component("RadarCardProfit",{"../api":{AUTH_SESSION_ENDING_EVENT:"end",fetchCompetitorDetail:()=>{requests++;return new Promise((r)=>{resolve=r})}}});
  const root=node("root");const app=renderer.createApp(Profit,{item,storeScope:"all"});app.mount(root);
  assert.equal(requests,0);const button=walk(root).find((n)=>n.tag==="button")!;
  button.props.onClick({stopPropagation(){}});button.props.onClick({stopPropagation(){}});assert.equal(requests,1);
  resolve({own_store_profitability:{items:[row(62,12)]}});await new Promise(setImmediate);await vue.nextTick();
  assert.match(text(root),/62[,.]00/);assert.match(text(root),/12%/);assert.match(text(root),/1 \/ 1/);app.unmount();
});
test("scope changes and unmount cancel pending profit reads and reject late results",async()=>{
  let signal:AbortSignal|undefined;let resolve!:(data:any)=>void;
  const Profit=component("RadarCardProfit",{"../api":{AUTH_SESSION_ENDING_EVENT:"end",fetchCompetitorDetail:(_p:any,_s:any,_e:any,_scope:any,s:AbortSignal)=>{signal=s;return new Promise((r)=>{resolve=r})}}});
  const scope=vue.ref("all");const root=node("root");const app=renderer.createApp({setup:()=>()=>vue.h(Profit,{item,storeScope:scope.value})});app.mount(root);
  walk(root).find((n)=>n.tag==="button")!.props.onClick({stopPropagation(){}});scope.value="current";await vue.nextTick();assert.equal(signal!.aborted,true);
  resolve({own_store_profitability:{items:[row(888,99)]}});await new Promise(setImmediate);await vue.nextTick();assert.doesNotMatch(text(root),/888/);
  walk(root).find((n)=>n.tag==="button")!.props.onClick({stopPropagation(){}});app.unmount();assert.equal(signal!.aborted,true);
});

test("a failed profit refresh explains retained results and allows a later retry",async()=>{
  let requests=0;
  const Profit=component("RadarCardProfit",{"../api":{AUTH_SESSION_ENDING_EVENT:"end",fetchCompetitorDetail:async()=>{
    requests++;
    if(requests===2)throw new Error("服务暂不可用");
    return {own_store_profitability:{items:[row(requests===1?62:70,12)]}};
  }}});
  const root=node("root");const app=renderer.createApp(Profit,{item,storeScope:"all"});app.mount(root);
  const click=async()=>{walk(root).find((n)=>n.tag==="button")!.props.onClick({stopPropagation(){}});await new Promise(setImmediate);await vue.nextTick()};
  await click();await click();
  assert.match(text(root),/62[,.]00/);assert.match(text(root),/更新失败，以上保留上次结果：服务暂不可用/);
  await click();assert.match(text(root),/70[,.]00/);assert.doesNotMatch(text(root),/更新失败/);app.unmount();
});
