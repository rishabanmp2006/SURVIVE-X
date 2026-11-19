// UNO Q integration scaffold. No display, input or protocol decoder is enabled.
// A confirmed TFT, pins, compatible display driver and target SDK are required.
#include <Arduino_RouterBridge.h>

constexpr unsigned MaxRecord = 639;
constexpr unsigned MaxPart = 80;
constexpr unsigned MaxParts = 8;
char recordBuffer[MaxRecord + 1];
unsigned recordLength = 0, nextPart = 0, partCount = 0;
bool recordReady = false;

// Called through Arduino Router RPC. Keep this bounded and in loop context.
bool acceptPart(int index, int count, String fragment) {
  if (recordReady || index < 0 || count < 1 || count > MaxParts ||
      index >= count || fragment.length() > MaxPart) return false;
  if (index == 0) { recordLength = 0; nextPart = 0; partCount = count; }
  if (unsigned(index) != nextPart || unsigned(count) != partCount ||
      recordLength + fragment.length() > MaxRecord) {
    recordLength = nextPart = partCount = 0;
    return false;
  }
  for (unsigned i = 0; i < fragment.length(); ++i) {
    char c = fragment[i];
    if (c < 32 || c > 126) { recordLength = nextPart = partCount = 0; return false; }
    recordBuffer[recordLength++] = c;
  }
  ++nextPart;
  if (nextPart == partCount) {
    recordBuffer[recordLength] = '\0';
    recordReady = true;
  }
  return true;
}

void setup() {
  Bridge.begin();
  Bridge.provide_safe("sx_part", acceptPart);
  // No successful frame ACK is emitted by this scaffold.
  Bridge.notify("sx_event", "boot", "", 0, "", 1);
}

void loop() {
  if (recordReady) {
    // TODO: feed recordBuffer to a bounded, tested profile 3 parser. The
    // Wokwi parser depends on ESP32/Adafruit SPI and must be separated first.
    recordReady = false;
    recordLength = nextPart = partCount = 0;
  }
}
