// Native test doubles for GPIO, clock, UART and TFT calls only.
#pragma once
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>
using std::min;
using std::max;
enum { A0,A1,D2,D4,D9,D10,RX,TX,INPUT_PULLUP,SERIAL_8N1,ILI9341_YELLOW };
uint32_t ticks=0;
int pins[]={2048,2048,1,1};
uint32_t millis() { return ticks; }
void delay(int n) { ticks+=n; }
void analogReadResolution(int) {}
void pinMode(int,int) {}
int digitalRead(int pin) { return pins[pin]; }
int analogRead(int pin) { return pins[pin]; }
struct {
  std::string log, input;
  size_t write(uint8_t c) { log+=c; return 1; }
  size_t write(const uint8_t *s,size_t n) { log.append((const char*)s,n); return n; }
  void begin(int,int,int,int) {}
  void println() { log+='\n'; }
  int available() { return input.size(); }
  int read() { int c=input[0]; input.erase(0,1); return c; }
} Serial;
struct Adafruit_ILI9341 {
  int spans=0,x=0,y=0,size=1;
  std::vector<std::string> labels;
  Adafruit_ILI9341(int,int) {}
  void begin() {}
  void setRotation(int n) { assert(n==1); }
  void setTextWrap(bool w) { assert(!w); }
  void fillScreen(int) { labels.clear(); spans=0; }
  void setCursor(int cx,int cy) { x=cx; y=cy; }
  void setTextColor(int) {}
  void setTextSize(int n) { size=n; }
  void print(const char *s) {
    assert(x>=0 && x+strlen(s)*6*size<=320 && y>=0 && y+8*size<=240);
    labels.emplace_back(s);
  }
  void fillRect(int x,int y,int w,int h,int) { assert(x>=0 && y>=0 && x+w<=320 && y+h<=240); }
  void drawRect(int x,int y,int w,int h,int) { assert(x>=0 && y>=0 && x+w<=320 && y+h<=240); }
  void drawLine(int x1,int y1,int x2,int y2,int) { assert(x1>=0 && x1<320 && x2>=0 && x2<320 && y1>=0 && y1<240 && y2>=0 && y2<240); }
  void drawFastHLine(int x,int y,int w,int) {
    assert(x>=0 && w>0 && x+w<=320 && y>=0 && y<240); ++spans;
  }
};
