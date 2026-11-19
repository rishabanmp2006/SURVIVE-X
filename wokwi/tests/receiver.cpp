#include "../wokwi.ino"
#include <iostream>

void move(int pin,int value) {
  pins[pin]=value; loop(); ticks+=31; loop();
  pins[pin]=pin==A0 || pin==A1 ? 2048 : 1;
  loop(); ticks+=31; loop();
}

int main() {
  setup();
  assert(tft.spans>0 && !menu);
  move(A0,0); assert(step==1);
  move(A0,4095); assert(step==0);
  move(D4,0); assert(menu && !tft.spans);
  move(A1,0); assert(cursor==1);
  move(A1,4095); assert(cursor==0);
  move(A1,0); move(D4,0); assert(!menu && place==1 && tft.spans);
  move(D4,0); move(A1,4095); move(D2,0); assert(!menu && place==1);
  ticks+=4000; loop(); assert(step==0 && place==1);
  pins[D4]=0; loop(); // First sampled edge opens Places, including a short tap.
  ticks+=800; loop(); assert(menu);
  pins[D4]=1; loop(); ticks+=5; pins[D4]=0; loop(); assert(menu);
  pins[D4]=1; loop(); ticks+=31; loop();
  pins[A1]=0; loop(); unsigned highlight=cursor;
  ticks+=2500; loop(); assert(cursor==highlight && menu);
  pins[A1]=2048; loop(); ticks+=31; loop();
  move(A0,4095); assert(!menu);
  move(D4,0); move(A1,0); move(A0,0); assert(!menu && place==highlight);
  serialLine("demo"); ticks+=2000; loop(); assert(step==1);
  serialLine("pause"); ticks+=4000; loop(); assert(step==1);
  move(A0,4095); assert(step==0);
  serialLine("resume");
  bool arrow[]={1,1,0,1,0,0,0,1};
  for (unsigned i=1; i<=8; ++i) {
    ticks+=2000; loop();
    assert(step==i%8 && (tft.spans>0)==arrow[step]);
  }
  // First external stream supplies authoritative navigation until a local pick.
  place=0; destinationPinned=false;
  std::string line;
  while (std::getline(std::cin,line)) {
    if (line.rfind("LABEL:",0)==0) {
      assert(std::find(tft.labels.begin(),tft.labels.end(),line.substr(6))!=tft.labels.end());
      continue;
    }
    if (line=="EXTINPUT") {
      bool oldMenu=menu; unsigned oldCursor=cursor;
      size_t oldLength=Serial.log.size();
      move(D4,0);
      assert(menu==oldMenu && cursor==oldCursor);
      assert(Serial.log.substr(oldLength).find("SELECT")!=std::string::npos);
      continue;
    }
    if (line=="OPEN") { move(D4,0); assert(menu); continue; }
    if (line=="DOWN") { move(A1,0); continue; }
    if (line=="PICK") { move(D4,0); assert(!menu); continue; }
    if (line=="BACK") { move(D2,0); assert(!menu); continue; }
    if (line=="MENU?") {
      assert(menu && std::find(tft.labels.begin(),tft.labels.end(),"OFFLINE PLACES")!=tft.labels.end());
      continue;
    }
    if (line=="TIMEOUT") { ticks+=1001; loop(); }
    else {
      Serial.input=line+"\n";
      while (Serial.available()) loop();
    }
    if (line=="TIMEOUT" || line.find("\"kind\":\"frame_end\"")!=std::string::npos)
      std::cout<<lastFrame<<" "<<linkLost<<" "<<(tft.spans>0)<<"\n";
  }
}
