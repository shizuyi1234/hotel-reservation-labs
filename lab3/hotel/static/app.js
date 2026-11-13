"use strict";

const main = document.querySelector("#main");
const nav = document.querySelector("#nav");
const modal = document.querySelector("#dialog");
const modalContent = document.querySelector("#dialog-content");
const roles = {guest: "顾客", staff: "酒店员工", admin: "平台管理员"};
const statusNames = {
  pending: "待确认", confirmed: "已确认", staying: "已入住",
  completed: "已完成", cancelled: "已取消", rejected: "已拒绝", expired: "已超时",
};
const eventNames = {
  book: "创建预订", modify: "改订", cancel: "取消", confirm: "酒店确认",
  reject: "拒单", expire: "确认超时", no_show: "未到店", check_in: "入住",
  check_out: "退房",
};
let actor = null;
let renderId = 0;
let resultsCache = null;

function node(tag, attrs = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "on") element.addEventListener("click", value);
    else if (value !== null && value !== false) element.setAttribute(key, value === true ? "" : value);
  }
  element.append(...children.flat().filter(value => value !== null && value !== undefined));
  return element;
}

function day(offset = 0) {
  const date = new Date(Date.now() + offset * 86400000);
  return new Intl.DateTimeFormat("sv-SE", {timeZone: "Asia/Shanghai"}).format(date);
}

const yuan = cents => "¥" + (Number(cents) / 100).toFixed(2);
const label = value => statusNames[value] || value;
const at = value => value ? value.slice(0, 19).replace("T", " ") : "—";
const freshKey = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), byte =>
  byte.toString(16).padStart(2, "0")).join("");

async function request(command, args = []) {
  const response = await fetch("/api", {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Hotel-Web": "1"},
    body: JSON.stringify({command, args: args.map(String)}),
  });
  const result = await response.json();
  if (!result.ok) {
    if (result.code === "auth" && actor) {
      actor = null;
      navigation(location.hash.slice(1));
    }
    throw Object.assign(new Error(result.message), result);
  }
  return Object.hasOwn(result, "result") ? result.result : result;
}

function notice(error, box) {
  const fields = error.code === "invalid" && error.details ? Object.values(error.details) : [];
  box.replaceChildren(node("p", {class: "notice error", role: "alert"},
    [error.message, ...fields].filter(Boolean).join("。")));
}

function button(text, callback, kind = "secondary") {
  const control = node("button", {type: "button", class: kind});
  control.textContent = text;
  control.addEventListener("click", async () => {
    const output = control.closest("section, dialog, details")?.querySelector(".feedback")
      || document.querySelector("#main > .feedback");
    control.disabled = true;
    try { await callback(); }
    catch (error) { if (output) notice(error, output); }
    finally { control.disabled = false; }
  });
  return control;
}

function panel(title, ...content) {
  return node("section", {class: "panel"}, node("h2", {}, title), ...content);
}

function feedback() { return node("div", {class: "feedback", "aria-live": "polite"}); }

function fields(name, title, options = {}) { return {name, title, ...options}; }
const text = (name, title, value = "", required = true) => fields(name, title, {value, required});
const number = (name, title, value = "", min = 1, required = true) =>
  fields(name, title, {type: "number", value, min, required, step: 1});
const date = (name, title, value = day(), required = true) =>
  fields(name, title, {type: "date", value, required});
const select = (name, title, choices, value = "") => fields(name, title, {choices, value});
const statusFilter = select("status", "订单状态", [["", "全部"], ...Object.entries(statusNames)]);
const money = (name, title, value = "", required = true) =>
  fields(name, title, {type: "number", value, min: 0, required, step: ".01"});
const cents = value => Math.round(Number(value) * 100);
const credentials = [
  text("username", "账号"),
  fields("password", "密码", {type: "password", required: true}),
];

function form(schema, title, submit) {
  const box = feedback();
  const submitButton = node("button", {type: "submit"}, title);
  const inputs = schema.map(({name, title: caption, choices, value, ...attrs}) => {
    let input;
    if (choices) {
      input = node("select", {name}, choices.map(([key, description]) =>
        node("option", {value: key}, description)));
    } else {
      input = node("input", {name, ...attrs});
    }
    input.value = value ?? "";
    return node("label", {}, caption, input);
  });
  const result = node("form", {}, node("div", {class: "fields"}, inputs), submitButton, box);
  result.addEventListener("submit", async event => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(result));
    submitButton.disabled = true;
    box.replaceChildren();
    try { await submit(values); }
    catch (error) { notice(error, box); }
    finally { submitButton.disabled = false; }
  });
  return result;
}

function table(headings, rows) {
  if (!rows.length) return node("p", {class: "muted"}, "没有匹配的记录。");
  return node("div", {class: "scroll"}, node("table", {},
    node("thead", {}, node("tr", {}, headings.map(heading => node("th", {scope: "col"}, heading)))),
    node("tbody", {}, rows.map(row => node("tr", {}, row.map(cell =>
      node("td", {}, cell instanceof Node ? cell : String(cell ?? "—"))))))));
}

function popup(title, ...contents) {
  modalContent.replaceChildren(node("h2", {}, title), feedback(), ...contents);
  if (!modal.open) modal.showModal();
}

function go(page) {
  if (location.hash === "#" + page) render();
  else location.hash = page;
}

function filters(data, mapping) {
  return Object.entries(mapping).flatMap(([key, flag]) => data[key] ? [flag, data[key]] : []);
}

function noticeOK(box, message) {
  box.replaceChildren(node("p", {class: "notice", role: "status"}, message));
}

function navigation(page) {
  const options = [["search", "查找酒店"]];
  if (!actor) options.push(["login", "登录"], ["register", "注册"]);
  else {
    options.push(["orders", actor.role === "guest" ? "我的订单" : "订单工作台"]);
    if (actor.role !== "guest") options.push(["manage", "酒店管理"], ["stats", "统计与日志"]);
  }
  nav.replaceChildren(...options.map(([key, caption]) =>
    node("a", {href: "#" + key, "aria-current": page === key ? "page" : null}, caption)));
  if (actor) nav.append(node("span", {}, actor.username + " · " + roles[actor.role]),
    button("退出", async () => {
      await request("logout"); actor = null; go("search");
    }));
}

function stayArgs(values) {
  return [values.arrival, values.departure, values.people, values.quantity];
}

function stayFields(values = {}) {
  return [
    date("arrival", "入住日", values.arrival || day(1)),
    date("departure", "退房日", values.departure || day(2)),
    number("people", "人数", values.people || 2),
    number("quantity", "间数", values.quantity || 1),
  ];
}

function quoteDialog(quote, order = null) {
  const stay = quote.stay;
  const args = [quote.room_id, ...stayArgs(stay), quote.token];
  const key = freshKey();
  popup(order ? "确认改订报价" : "确认预订报价",
    node("p", {}, quote.hotel_name + " · " + quote.room_name),
    table(["住宿日", "每间每晚", "间数", "小计"], quote.nights.map(n =>
      [n.day, yuan(n.price), n.quantity, yuan(n.subtotal)])),
    node("p", {class: "price"}, "合计 " + yuan(quote.total)),
    quote.difference === undefined ? null : node("p", {}, "与原订单差额 " + yuan(quote.difference)),
    button(order ? "提交改订" : "提交预订", async () => {
      try {
        await request(order ? "modify" : "book", [
          ...(order ? [order.id, quote.version ?? order.version] : []),
          ...args, "--request-id", key,
        ]);
        modal.close();
        go("orders");
      } catch (error) {
        if (error.code !== "requote") throw error;
        quoteDialog(error.details, order);
        notice(error, modalContent.querySelector(".feedback"));
      }
    }, ""));
}

function searchPage() {
  const results = node("div");
  function show(hits) {
    results.replaceChildren(node("h2", {}, "可订房型"), hits.length ?
      node("div", {class: "cards"}, hits.map(quote => panel(quote.hotel_name,
        node("p", {}, quote.room_name),
        node("p", {class: "muted"}, quote.stay.arrival + " 至 " + quote.stay.departure),
        node("p", {class: "price"}, yuan(quote.total)),
        actor?.role === "guest" ? button("查看逐晚报价", () => quoteDialog(quote), "")
          : actor ? null : node("a", {href: "#login"}, "登录后预订"),
      ))) : node("p", {}, "当前条件下没有可订房型。"));
  }
  const previous = resultsCache?.values;
  if (resultsCache) show(resultsCache.hits);
  return [
    node("section", {class: "hero"}, node("h1", {}, "找到合适的住宿"),
      node("p", {}, "按入住日期查询真实库存，确认每晚价格后再预订。")),
    panel("住宿条件", form([
      text("city", "城市", previous?.city || "南京"), ...stayFields(previous),
      text("room", "房型", previous?.room || "", false),
      money("min", "每晚最低价（元）", previous?.min || "", false),
      money("max", "每晚最高价（元）", previous?.max || "", false),
    ], "搜索酒店", async data => {
      const hits = await request("search", [data.city, ...stayArgs(data),
        ...filters(data, {room: "--room-name"}),
        ...(data.min ? ["--minimum", cents(data.min)] : []),
        ...(data.max ? ["--maximum", cents(data.max)] : []),
      ]);
      resultsCache = {values: data, hits};
      show(hits);
    })), results,
  ];
}

function loginPage(register = false) {
  return [panel(register ? "注册顾客账号" : "登录栖程酒店",
    form(credentials, register ? "注册并登录" : "登录", async data => {
      if (register) await request("register", [data.username, data.password]);
      actor = await request("login", [data.username, data.password]);
      go(actor.role === "guest" ? "search" : "orders");
    }))];
}

async function orderDialog(id) {
  const order = await request("order", [id]);
  const controls = node("div", {class: "actions"});
  const mutate = async (command, extra = []) => {
    await request(command, [id, order.version, ...extra, "--request-id", freshKey()]);
    modal.close();
    go("orders");
  };
  if (actor?.role === "guest" && ["pending", "confirmed"].includes(order.status)) {
    controls.append(
      button("修改预订", async () => {
        popup("修改预订", form([
          number("room", "新房型编号", order.room_id), ...stayFields(order),
        ], "查看新报价", async values => {
          const quote = await request("quote", [values.room, ...stayArgs(values), "--order", id]);
          quoteDialog(quote, order);
        }));
      }),
      button("取消预订", () => popup("确认取消订单 #" + id,
        node("p", {}, "取消后将释放此订单尚未使用的预留房间。"),
        button("确认取消", () => mutate("cancel"), "danger")), "danger"),
    );
  }
  if (actor?.role === "staff") {
    if (order.status === "pending") controls.append(
      button("确认订单", () => mutate("confirm"), ""),
      button("拒绝订单", () => popup("拒绝订单 #" + id,
        form([text("reason", "拒单原因")], "确认拒单", values =>
          mutate("reject", [values.reason]))), "danger"),
    );
    if (order.status === "confirmed") controls.append(button("办理入住", () => mutate("check-in"), ""));
    if (order.status === "staying") controls.append(button("办理退房", () => mutate("check-out"), ""));
  }
  popup("订单 #" + id,
    node("p", {}, order.hotel_name + " · " + order.room_name + " · " + label(order.status)),
    node("p", {}, order.arrival + " 至 " + order.departure + " · " + yuan(order.total)),
    order.status === "pending" ? node("p", {}, "确认截止 " + at(order.deadline)) : null,
    controls,
    node("h3", {}, "逐晚记录"),
    table(["版本", "住宿日", "间数", "每间价格", "占用"], order.nights.map(n =>
      [n.revision, n.day, n.quantity, yuan(n.price), ({reserved:"预留",used:"已用",released:"释放"})[n.usage]])),
    node("h3", {}, "订单历史"),
    table(["时间", "操作", "结果", "说明"], order.events.map(e =>
      [at(e.at), eventNames[e.action] || e.action, label(e.after_status), e.reason])),
  );
}

async function ordersPage() {
  const output = node("div");
  async function refresh(data = {}) {
    const rows = await request("orders", filters(data, {
      hotel:"--hotel", id:"--order-id", room:"--room", status:"--status",
      from:"--stay-from", to:"--stay-to", created:"--created-from", until:"--created-to",
    }));
    output.replaceChildren(table(["订单", "酒店 / 房型", "住宿日期", "总价", "状态", "操作"],
      rows.map(o => [o.id, o.hotel_name + " / " + o.room_name, o.arrival + " 至 " + o.departure,
        yuan(o.total), node("span", {class:"badge"}, label(o.status)),
        button("详情", () => orderDialog(o.id))])));
  }
  const ui = panel(actor.role === "guest" ? "我的订单" : "订单工作台",
    form([statusFilter, number("id","订单编号","",1,false), number("hotel","酒店编号","",1,false),
      number("room","房型编号","",1,false), date("from","入住区间开始","",false),
      date("to","入住区间结束","",false), date("created","创建日期起","",false),
      date("until","创建日期止","",false)], "查询订单", refresh), feedback(), output);
  try { await refresh(); } catch (error) { notice(error, ui.querySelector(":scope > .feedback")); }
  return [ui];
}

function action(title, schema, command, args) {
  const output = feedback();
  return node("details", {}, node("summary", {}, title), form(schema, title, async data => {
    const result = await request(command, args(data));
    const id = result && (result.hotel_id || result.room_id || result.closure_id);
    noticeOK(output, id ? "已保存，编号 #" + id : "操作成功。");
  }), output);
}

function listing(title, schema, command, args, columns, convert) {
  const output = node("div");
  return panel(title, form(schema, "查询", async data => {
    const rows = await request(command, args(data));
    output.replaceChildren(table(columns, rows.map(convert)));
  }), output);
}

const yesNo = (name, caption, value = "no") =>
  select(name, caption, [["yes", "是"], ["no", "否"]], value);

async function managePage() {
  const hotels = await request("hotels");
  const hotel = number("hotel", "酒店编号", hotels[0]?.id || "");
  const room = number("room", "房型编号", 1);
  const hotelTable = table(["编号", "酒店", "城市", "地址", "联系方式"],
    hotels.map(h => [h.id,h.name,h.city,h.address,h.contact]));
  const content = [panel("授权酒店", hotelTable, button("刷新列表", () => go("manage")))];
  const profile = [text("name","酒店名称"),text("city","城市"),text("address","地址"),
    text("contact","联系方式")];
  const profileArgs = data => [data.name,data.city,data.address,data.contact];
  if (actor.role === "admin") {
    content.push(
      action("登记酒店", profile, "hotel-save", profileArgs),
      action("授予账号权限", [text("username","账号"), select("role","角色",
        [["staff","酒店员工"],["admin","平台管理员"]],"staff"),
        number("hotel","授权酒店编号","",1,false)], "grant", data =>
        [data.username,data.role,...filters(data,{hotel:"--hotel"})]),
      action("撤销酒店授权", [number("user","用户编号"), hotel], "revoke", data => [data.user,data.hotel]),
      action("启用或停用账号", [number("user","用户编号"), yesNo("enabled","启用账号","yes")],
        "account-state", data => [data.user,data.enabled]),
      listing("账号与授权", [], "accounts", () => [],
        ["编号","账号","角色","状态","授权酒店"], a =>
          [a.id,a.username,roles[a.role],a.enabled ? "启用" : "停用",a.hotels]),
      action("登记停业区间", [hotel,date("first","开始日"),date("end","结束日",day(1)),
        text("reason","停业原因")], "closure-add", data => [data.hotel,data.first,data.end,data.reason]),
      action("恢复营业", [number("closure","停业记录编号")], "closure-end", data => [data.closure]),
    );
  }
  content.push(action("更新酒店资料", [hotel,...profile], "hotel-save",
    data => [...profileArgs(data),"--hotel",data.hotel]));
  if (actor.role === "staff") {
    content.push(
      action("新增或更新房型", [hotel,text("name","房型"),text("description","说明"),
        number("capacity","每间容量"),number("room","现有房型编号，可留空","",1,false)],
        "room-save", data => [data.hotel,data.name,data.description,data.capacity,
          ...filters(data,{room:"--room"})]),
      action("启用或停用房型", [room,yesNo("enabled","启用房型","yes")],
        "room-state", data => [data.room,data.enabled]),
      action("维护日历库存", [room,date("day","住宿日"),number("total","总间数",5,0),
        money("price","每晚价格（元）",260),yesNo("stopped","停售")], "inventory-save",
        data => [data.room,data.day,data.total,cents(data.price),"--stopped",data.stopped]),
      action("登记受影响订单处置", [number("case","处置编号"),text("contact","联系记录"),
        text("decision","处理决定")], "case-resolve", data => [data.case,data.contact,data.decision]),
    );
  }
  content.push(
    listing("酒店房型", [hotel], "rooms", data => [data.hotel],
      ["编号","房型","说明","容量","状态"], r =>
        [r.id,r.name,r.description,r.capacity,r.enabled ? "启用" : "停用"]),
    listing("日历库存", [room,date("first","开始日"),date("end","结束日",day(7))], "inventory",
      data => [data.room,data.first,data.end], ["日期","总间数","有效占用","余量","价格","停售"],
      row => [row.day,row.total,row.occupied,row.available,yuan(row.price),row.stopped ? "是" : "否"]),
    listing("停业记录", [hotel], "closures", data => [data.hotel],
      ["编号","开始日","结束日","原因","状态"], c =>
        [c.id,c.first_day,c.end_day,c.reason,c.active ? "有效" : "已解除"]),
    listing("受影响订单处置", [hotel,yesNo("history","包含已处理")], "cases",
      data => [data.hotel,...(data.history === "yes" ? ["--all"] : [])],
      ["编号","订单","订单状态","来源","联系记录","处理决定"], c =>
        [c.id,c.order_id,label(c.status),c.source,c.contact_note,c.decision]),
  );
  return content;
}

function statsPage() {
  const output = node("div");
  const hotel = number("hotel","酒店编号，可留空","",1,false);
  const stats = panel("经营统计", form([hotel,number("room","房型编号，可留空","",1,false),
    date("first","开始日",day(-7)),date("end","结束日",day(1))], "统计", async data => {
    const result = await request("statistics", [data.first,data.end,
      ...filters(data,{hotel:"--hotel",room:"--room"}),"--include-rows"]);
    output.replaceChildren(table(["新订单","取消单","预留间夜","已用间夜","预计房费"],
      [[result.new_orders,result.cancelled,result.reserved_nights,result.used_nights,
        yuan(result.estimated_revenue)]]),
      node("h3",{},"间夜明细"),table(["订单","日期","间数","单价","类型"],result.nights.map(n =>
        [n.order_id,n.day,n.quantity,yuan(n.price),n.usage === "used" ? "已用" : "预留"])));
  }), output);
  const audit = listing("操作日志", [hotel,number("actor","用户编号，可留空","",1,false),
    date("first","开始日",day(-7)),date("end","结束日",day(1)),text("target","操作对象","",false),
    text("result","结果代码","",false)], "audit", data => filters(data,{
      hotel:"--hotel",actor:"--actor-id",first:"--first",end:"--end",target:"--target",result:"--result",
    }), ["时间","操作者","操作","对象","结果","说明"], a =>
      [at(a.at),a.actor_id,eventNames[a.action] || a.action,a.target,a.result,a.detail]);
  return [stats, audit];
}

async function render() {
  const generation = ++renderId;
  const page = location.hash.slice(1) || "search";
  navigation(page);
  main.replaceChildren(node("p", {role:"status"}, "正在加载…"));
  try {
    let content;
    if (page === "login" || page === "register") content = loginPage(page === "register");
    else if (page === "orders" && actor) content = await ordersPage();
    else if (page === "manage" && actor && actor.role !== "guest") content = await managePage();
    else if (page === "stats" && actor && actor.role !== "guest") content = statsPage();
    else content = searchPage();
    if (generation === renderId) main.replaceChildren(...content, feedback());
  } catch (error) {
    if (generation !== renderId) return;
    const box = feedback();
    main.replaceChildren(box);
    notice(error, box);
  }
}

window.addEventListener("hashchange", render);
request("session").then(user => {actor = user;}).catch(() => {}).finally(render);
