#include <Adafruit_ILI9341.h>
#include <ArduinoJson.h>
#include "replay.h"

#if ARDUINO_USB_CDC_ON_BOOT
#define Port Serial0
#else
#define Port Serial
#endif

Adafruit_ILI9341 tft(D10, D9);
constexpr unsigned MaxChunks = 128, LegacyChunks = 16, MaxPayload = 160, Watchdog = 1000;
char pending[MaxChunks][MaxPayload + 1], session[24] = "flash";
unsigned expected, received, frameId, lastFrame, lastSeq, outputSeq;
unsigned place, cursor, step;
bool menu, external, negotiated, linkLost;
bool demo, traceInput, destinationPinned, extended, incremental, frameDelta;
unsigned frameBase;
char frameScreen[20] = "NAVIGATION", cachedScreen[20] = "NAVIGATION";
uint64_t hostDestination, frameDestination, cachedDestination;
char cached[MaxChunks][MaxPayload + 1];
unsigned cachedCount;
const char *feedback = "READY";
const char *lossReason = "WAITING FOR FRAME";
uint32_t lastGood, lastStep, frameStarted;

void emitEvent(const char *kind, const char *key, const char *value) {
  JsonDocument msg;
  msg["v"] = 1;
  msg["session"] = "adapter";
  msg["seq"] = ++outputSeq;
  msg["t_ms"] = millis();
  msg["kind"] = kind;
  msg["body"][key] = value;
  if (!strcmp(kind,"ack")) msg["body"]["frame_id"] = lastFrame;
  serializeJson(msg, Port);
  Port.println();
}

void statusLine() {
  tft.setCursor(8, 231);
  tft.setTextSize(1);
  tft.setTextColor(ILI9341_YELLOW);
  tft.print(external ? "INTERACTIVE / UART  " : demo ? "DEMO REPLAY  " : "INTERACTIVE  ");
  tft.print(feedback);
}

void unavailable(const char *reason) {
  tft.fillScreen(0);
  tft.setCursor(8, 96);
  tft.setTextSize(2);
  tft.setTextColor(ILI9341_YELLOW);
  tft.print("GUIDANCE UNAVAILABLE");
  tft.setCursor(8, 126);
  tft.setTextSize(1);
  tft.print(reason);
  statusLine();
}

void invalidate(const char *reason) {
  expected = cachedCount = 0;
  lossReason = reason;
  if (!linkLost) {
    if (!menu && (!extended || !strcmp(cachedScreen,"NAVIGATION"))) unavailable(reason);
  }
  emitEvent("resync","reason",reason);
  linkLost = true;
}

bool number(JsonVariantConst v, int limit) {
  return v.is<int>() && v.as<int>() >= 0 && v.as<int>() < limit;
}

// Seven vertices of the Stage 4 arrow. Rasterize the supplied outline by scanline.
void fillPolygon(JsonArrayConst op) {
  int x[7], y[7], lo=239, hi=0;
  for (int i=0; i<7; ++i) {
    x[i]=op[i+2][0]; y[i]=op[i+2][1];
    lo=min(lo,y[i]); hi=max(hi,y[i]);
  }
  for (int row=lo; row<=hi; ++row) {
    int cuts[7], count=0;
    for (int i=0,j=6; i<7; j=i++) {
      if ((y[i]>row) == (y[j]>row)) continue;
      int cross=x[i]+(row-y[i])*(x[j]-x[i])/(y[j]-y[i]);
      int pos=count++;
      while (pos && cuts[pos-1]>cross) { cuts[pos]=cuts[pos-1]; --pos; }
      cuts[pos]=cross;
    }
    for (int k=1; k<count; k+=2)
      tft.drawFastHLine(cuts[k-1],row,cuts[k]-cuts[k-1]+1,op[1].as<int>());
  }
}

#ifdef SURVIVEX_TEST_FAULTS
int paintFaultCountdown = -1;
#endif
bool primitive(const char *line, bool paint) {
#ifdef SURVIVEX_TEST_FAULTS
  if (paint && paintFaultCountdown >= 0 && paintFaultCountdown-- == 0) return false;
#endif
  JsonDocument doc;
  if (deserializeJson(doc,line)) return false;
  JsonArrayConst op=doc.as<JsonArrayConst>();
  if (incremental && op[0]=="b" && op.size()>=2) {
    for (unsigned i=1; i<op.size(); ++i) {
      JsonArrayConst item=op[i].as<JsonArrayConst>();
      if (item.isNull() || item[0]=="b") return false;
      char child[MaxPayload+1];
      if (measureJson(item)>MaxPayload) return false;
      serializeJson(item,child,sizeof(child));
      if (!primitive(child,paint)) return false;
    }
    return true;
  }
  if (op[0]=="r" && extended && op.size()==7 && number(op[1],320) && number(op[2],240) &&
      number(op[3],321) && number(op[4],241) && number(op[5],65536) &&
      (op[6].isNull() || number(op[6],65536))) {
    int x=op[1], y=op[2], w=op[3], h=op[4];
    if (!w || !h || x+w>320 || y+h>240) return false;
    if (paint) {
      tft.fillRect(x,y,w,h,op[5].as<int>());
      if (!op[6].isNull()) tft.drawRect(x,y,w,h,op[6].as<int>());
    }
    return true;
  }
  if (op[0]=="l" && extended && op.size()==6 && number(op[1],320) && number(op[2],240) &&
      number(op[3],320) && number(op[4],240) && number(op[5],65536)) {
    if (paint) tft.drawLine(op[1],op[2],op[3],op[4],op[5]);
    return true;
  }
  if (op[0]=="p" && op.size()==9 && number(op[1],65536)) {
    for (unsigned i=2; i<9; ++i)
      if (op[i].size()!=2 || !number(op[i][0],320) || !number(op[i][1],240)) return false;
    if (paint) fillPolygon(op);
    return true;
  }
  if (op[0]!="t" || op.size()!=6 || !number(op[1],320) || !number(op[2],240) ||
      !number(op[3],3) || !op[3].as<int>() || !number(op[4],65536) || !op[5].is<const char*>()) return false;
  JsonString str=op[5].as<JsonString>();
  int size=op[3], x=op[1], y=op[2];
  if (x+6*size*str.size()>320 || y+8*size>240) return false;
  for (unsigned i=0; i<str.size(); ++i) if (str.c_str()[i]<32 || str.c_str()[i]>126) return false;
  if (paint) {
    tft.setCursor(x,y); tft.setTextSize(size); tft.setTextColor(op[4].as<int>()); tft.print(str.c_str());
  }
  return true;
}

uint32_t crcByte(uint32_t crc, uint8_t byte) {
  crc ^= byte;
  for (int i=0; i<8; ++i) crc=(crc>>1)^((crc&1) ? 0xedb88320u : 0);
  return crc;
}

void renderCurrent();
void pollInput(uint32_t now);

bool commit(uint32_t wanted) {
  if (!expected || received!=expected) return false;
  uint32_t crc=~0u;
  if (extended) {
    for (const char *s=frameScreen; *s; ++s) crc=crcByte(crc,*s);
    crc=crcByte(crc,'\n');
    if (incremental) {
      const char *kind=frameDelta ? "DELTA" : "FULL";
      while (*kind) crc=crcByte(crc,*kind++);
      crc=crcByte(crc,'\n');
      char base[16]; snprintf(base,sizeof(base),"%u",frameBase);
      for (const char *s=base; *s; ++s) crc=crcByte(crc,*s);
      crc=crcByte(crc,'\n');
    }
  }
  for (unsigned i=0; i<received; ++i) {
    if (!primitive(pending[i],false)) return false;
    for (const char *s=pending[i]; *s; ++s) crc=crcByte(crc,*s);
    crc=crcByte(crc,'\n');
  }
  if (~crc!=wanted) return false;
  if (frameDelta && (linkLost || frameBase!=lastFrame || strcmp(frameScreen,cachedScreen))) return false;
  // Paint from the validated pending operations. Commit the baseline only after
  // every detectable parse/draw operation succeeds.
  if (!menu && destinationPinned && !extended && frameDestination!=placeIds[place]) {
    unavailable("WAITING FOR SELECTED DESTINATION");
  } else if (!menu) {
    if (!frameDelta) tft.fillScreen(0);
    for (unsigned i=0; i<received; ++i) {
      if (!primitive(pending[i],true)) {
        unavailable("DRAW FAILURE");
        return false;
      }
      if (incremental) pollInput(millis());
    }
  }
  if (!frameDelta) {
    memcpy(cached,pending,received*sizeof(cached[0]));
    cachedCount=received;
  }
  cachedDestination=frameDestination;
  strcpy(cachedScreen,frameScreen);
  expected=0; linkLost=false; lastGood=millis(); lastFrame=frameId;
  emitEvent("ack","session",session);
  return true;
}

// Flash UI reads its own immutable records, even during a partial UART frame.
bool renderFlash(const Frame &frame, bool paint) {
  const char *s=frame.ops;
  uint32_t crc=~0u;
  for (unsigned i=0; i<frame.chunks; ++i) {
    const char *end=strchr(s,'\n');
    if (!end || end-s>MaxPayload) return false;
    char op[MaxPayload+1];
    memcpy(op,s,end-s); op[end-s]=0;
    if (!primitive(op,paint)) return false;
    while (s<=end) crc=crcByte(crc,*s++);
  }
  return !*s && ~crc==frame.crc;
}

void renderCurrent() {
  if (external && !menu) {
    if (!cachedCount || linkLost) { unavailable(lossReason); return; }
    if (destinationPinned && !extended && cachedDestination!=placeIds[place]) {
      unavailable("WAITING FOR SELECTED DESTINATION"); return;
    }
    tft.fillScreen(0);
    for (unsigned i=0; i<cachedCount; ++i) {
      if (!primitive(cached[i],true)) {
        unavailable("DRAW FAILURE");
        invalidate("DRAW FAILURE");
        return;
      }
      if (incremental) pollInput(millis());
    }
    // The host owns all pixels of an external frame.
    return;
  }
  const Frame &f=frames[menu ? Places*Samples+cursor : place*Samples+step];
  if (!renderFlash(f,false)) { unavailable("BAD FLASH FRAME"); return; }
  tft.fillScreen(0);
  renderFlash(f,true);
  statusLine();
  if (!external) {
    ++lastFrame;
    emitEvent("ack","session",session);
  }
}

void packet(const char *line) {
  JsonDocument document;
  auto error=deserializeJson(document,line);
  JsonVariantConst msg=document.as<JsonVariantConst>();
  if (error || msg["v"]!=1 || !msg["seq"].is<unsigned>() ||
      !msg["t_ms"].is<unsigned>() || !msg["session"].is<const char*>() ||
      !msg["body"].is<JsonObjectConst>()) { invalidate("BAD ENVELOPE"); return; }
  const char *id=msg["session"];
  unsigned seq=msg["seq"];
  JsonVariantConst b=msg["body"];
  if (msg["kind"]=="hello") {
    if (negotiated && !strcmp(session,id) && seq<=lastSeq) return;
    negotiated=false; invalidate("WAITING FOR FRAME");
    if (!seq || !*id || strlen(id)>=sizeof(session) || b["width"]!=320 || b["height"]!=240 ||
        b["color"]!="RGB565" || b["font"]!="gfx-classic-6x8-ascii" ||
        b["max_payload"]!=MaxPayload || b["watchdog_ms"]!=Watchdog) return;
    if (!b["profile"].isNull() && b["profile"]!=2 && b["profile"]!=3) return;
    incremental=b["profile"]==3;
    extended=b["profile"]==2 || incremental;
    menu=false; destinationPinned=false;
    external=true; demo=false; hostDestination=0;
    strcpy(session,id); lastSeq=seq; lastFrame=0; negotiated=true; return;
  }
  if (!negotiated || strcmp(id,session)) { invalidate("HELLO REQUIRED"); return; }
  if (seq<=lastSeq) return;
  bool gap=seq-lastSeq!=1;
  lastSeq=seq;
  if (gap) invalidate("SEQUENCE GAP");
  if (incremental && msg["kind"]=="ping") {
    if (!linkLost && !expected && b["frame_id"]==lastFrame) {
      lastGood=millis(); emitEvent("ack","session",session);
    } else invalidate("PING BASE MISMATCH");
    return;
  }
  if (msg["kind"]=="frame_begin") {
    if (!b["frame_id"].is<unsigned>() || b["frame_id"].as<unsigned>()<=lastFrame ||
        (!incremental && !b["base_frame_id"].isNull()) || !number(b["chunks"],(extended ? MaxChunks : LegacyChunks)+1) || b["chunks"]==0) {
      invalidate("BAD FRAME BEGIN"); return;
    }
    frameDelta=false; frameBase=0;
    if (incremental) {
      if (b["mode"]=="DELTA" && b["base_frame_id"].is<unsigned>()) {
        frameDelta=true; frameBase=b["base_frame_id"].as<unsigned>();
        if (linkLost || !cachedCount || frameBase!=lastFrame) { invalidate("BASE MISMATCH"); return; }
      } else if (b["mode"]!="FULL" || !b["base_frame_id"].isNull()) {
        invalidate("BAD FRAME MODE"); return;
      }
    }
    if (extended) {
      const char *screen=b["screen"];
      if (!screen || (strcmp(screen,"HOME") && strcmp(screen,"NAVIGATION") &&
          strcmp(screen,"PLACES") && strcmp(screen,"AI_CHAT"))) {
        invalidate("BAD SCREEN"); return;
      }
      if (frameDelta && strcmp(screen,cachedScreen)) { invalidate("SCREEN MISMATCH"); return; }
      strcpy(frameScreen,screen);
    } else strcpy(frameScreen,"NAVIGATION");
    frameDestination=hostDestination;
    frameStarted=millis();
    expected=b["chunks"]; received=0; frameId=b["frame_id"]; return;
  }
  if (msg["kind"]=="draw") {
    JsonString payload=b["payload"].as<JsonString>();
    if (!expected || received>=expected || b["frame_id"]!=frameId || b["index"]!=received ||
        !payload || payload.size()>MaxPayload || strlen(payload.c_str())!=payload.size()) {
      invalidate("BAD DRAW CHUNK"); return;
    }
    strcpy(pending[received++],payload.c_str()); return;
  }
  if (msg["kind"]=="frame_end") {
    if (b["frame_id"]!=frameId || b["chunks"]!=received || !b["checksum"].is<uint32_t>() ||
        !commit(b["checksum"])) invalidate("INCOMPLETE OR BAD FRAME");
    return;
  }
  // Host owns sensors and navigation. Clear old guidance immediately on a loss record.
  if (msg["kind"]=="nav") hostDestination=b["destination_id"].as<uint64_t>();
  if (extended && strcmp(cachedScreen,"NAVIGATION")) return;
  if (msg["kind"]=="nav" && b["status"]!="OK") invalidate("NAVIGATION CHANGED");
  else if (msg["kind"]=="gps" && b["fix"]==false) invalidate("GPS FIX LOST");
}

const char *inputAction(int h, int v, bool select, bool back) {
  if (back) return "BACK";
  if (select) return "SELECT";
  if (v>3072) return "UP";
  if (v<1024) return "DOWN";
  if (h>3072) return "LEFT";
  if (h<1024) return "RIGHT";
  return "";
}

void dispatchAction(const char *action) {
  emitEvent("action","action",action);
  if (external && extended) return;
  feedback=action;
  if (menu) {
    if (!strcmp(action,"BACK") || !strcmp(action,"LEFT")) menu=false;
    else if (!strcmp(action,"SELECT") || !strcmp(action,"RIGHT")) {
      place=cursor; destinationPinned=true; menu=false;
    } else if (!strcmp(action,"DOWN")) cursor=(cursor+1)%Places;
    else if (!strcmp(action,"UP")) cursor=(cursor+Places-1)%Places;
  } else if (!strcmp(action,"SELECT") || !strcmp(action,"UP") || !strcmp(action,"DOWN")) {
    cursor=place; menu=true;
  } else if (!external) {
    if (!strcmp(action,"LEFT")) step=(step+Samples-1)%Samples;
    if (!strcmp(action,"RIGHT")) step=(step+1)%Samples;
  }
  if (demo) demo=false;
  lastStep=millis();
  renderCurrent();
}

void pollInput(uint32_t now) {
  static bool armed=true, releasing=false;
  static uint32_t releasedAt;
  int x=analogRead(A0), y=analogRead(A1), sel=digitalRead(D4), back=digitalRead(D2);
  const char *action=inputAction(x,y,!sel,!back);
  if (!*action) {
    if (!releasing) releasedAt=now;
    releasing=true;
    if (now-releasedAt>=30) armed=true;
    return;
  }
  releasing=false;
  if (!armed) return;
  armed=false;
  bool wasMenu=menu;
  dispatchAction(action);
  if (traceInput) {
    char info[128];
    snprintf(info,sizeof(info),"x=%d y=%d sel=%d back=%d %s %s->%s cursor=%u",
             x,y,sel,back,action,wasMenu?"PLACES":"NAV",menu?"PLACES":"NAV",cursor);
    emitEvent("debug","input",info);
  }
}

void serialLine(const char *line) {
  if (!strcmp(line,"trace on") || !strcmp(line,"trace off")) {
    traceInput=!strcmp(line,"trace on");
    if (traceInput) {
      char info[80];
      snprintf(info,sizeof(info),"x=%d y=%d sel=%d back=%d",
               analogRead(A0),analogRead(A1),digitalRead(D4),digitalRead(D2));
      emitEvent("debug","input",info);
    }
  } else if (!strcmp(line,"demo") || !strcmp(line,"pause") || !strcmp(line,"resume")) {
    demo=!external && strcmp(line,"pause")!=0;
    feedback=external ? "UART ACTIVE" : demo ? "RUNNING" : "PAUSED";
    lastStep=millis(); if (!external || !incremental) renderCurrent();
  } else packet(line);
}

void setup() {
  Port.begin(115200,SERIAL_8N1,RX,TX);
  pinMode(D4,INPUT_PULLUP); pinMode(D2,INPUT_PULLUP); analogReadResolution(12);
  tft.begin(); tft.setRotation(1); tft.setTextWrap(false);
  renderCurrent(); lastStep=millis();
}

void loop() {
  static char line[640]; static unsigned used; static bool overflow;
  for (unsigned n=0; n<256 && Port.available(); ++n) {
    int c=Port.read();
    if (c=='\n') {
      if (!overflow && used) { line[used]=0; serialLine(line); }
      used=0; overflow=false;
    } else if (c!='\r' && !overflow) {
      if (used+1<sizeof(line) && c>0) line[used++]=c;
      else { overflow=true; invalidate("OVERSIZE OR BAD INPUT"); }
    }
  }
  uint32_t now=millis();
  if (external && expected && now-frameStarted>=3000) invalidate("FRAME TIMEOUT");
  else if (external && !linkLost && now-lastGood>=Watchdog) {
    if (incremental && expected && frameDelta) invalidate("DELTA TIMEOUT");
    else if (extended && expected) {
      lossReason="LINK TIMEOUT";
      if (!strcmp(cachedScreen,"NAVIGATION")) unavailable(lossReason);
      if (!incremental) emitEvent("resync","reason",lossReason);
      linkLost=true;
    } else invalidate("LINK TIMEOUT");
  }
  pollInput(now);
  if (demo && !menu && millis()-lastStep>=2000) {
    step=(step+1)%Samples;
    if (step==Samples-1) place=(place+1)%Places;
    renderCurrent(); lastStep=now;
  }
  delay(1);
}
