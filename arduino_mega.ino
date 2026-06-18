// =============================================================================
//  UTV ECU — Arduino Mega 2560
//
//  MCP2515 #1  canBMS  CS=9   INT=3   — DALY BMS CAN        @ 500 Kbps
//  MCP2515 #2  canOUT  CS=10  INT=2   — Diagnostics / OBD-II @ 500 Kbps
//
//  Both modules share SPI bus: SCK=52, MOSI=51, MISO=50
//  Both modules MUST have an 8 MHz crystal.
//  Place 120Ω between CAN-H and CAN-L at each bus end.
//
//  Library: mcp_can by coryjfowler
//    Arduino IDE → Sketch → Include Library → Manage Libraries → "mcp_can"
// =============================================================================

#include <SPI.h>
#include <mcp_can.h>

// ---------------------------------------------------------------------------
//  CAN controllers
// ---------------------------------------------------------------------------
#define BMS_CS    9
#define OUT_CS   10

#define BMS_INT   3    // Mega INT1
#define OUT_INT   2    // Mega INT0

MCP_CAN canBMS(BMS_CS);
MCP_CAN canOUT(OUT_CS);

volatile bool bms_pending = false;
volatile bool out_pending = false;

void ISR_bms() { bms_pending = true; }
void ISR_out() { out_pending = true; }

// ---------------------------------------------------------------------------
//  ACS712 pins  (60 mV/A)
//  Index: 0=Reverse  1=Brake  2=Headlight  3=Hazard  4=Turn  5=Horn
// ---------------------------------------------------------------------------
const int ACS_PIN[6] = { A0, A1, A2, A3, A4, A5 };

const float ACS_SENS        = 0.060f;
const float ACS_NOISE_FLOOR = 0.10f;

float acsZero[6];

// ---------------------------------------------------------------------------
//  ECU data store
// ---------------------------------------------------------------------------
struct BMSData {
  float    cellV[19];
  float    packV, packA, soc;
  int8_t   temp[4];           // °C; -128 = NTC not fitted
  float    cellMin, cellMax;
  uint8_t  cellMinIdx, cellMaxIdx;
  uint8_t  chargeMOS, dischargeMOS;
  uint8_t  cellCount, ntcCount;
  uint16_t cycleCount;
  bool     faultPresent;
  uint8_t  faultBytes[7];
  bool     valid;
} bms;

struct ACSData {
  float current[6];
} acs;

// ---------------------------------------------------------------------------
//  Timing
// ---------------------------------------------------------------------------
unsigned long t_bmsPoll   = 0;
unsigned long t_diagTx    = 0;
unsigned long t_acsRead   = 0;
unsigned long t_debug     = 0;
unsigned long t_serialJSON = 0;

const unsigned long INTERVAL_BMS_POLL  = 1000;
const unsigned long INTERVAL_DIAG_TX   =  100;   // 10 Hz
const unsigned long INTERVAL_ACS_READ  =  200;   //  5 Hz
const unsigned long INTERVAL_DEBUG     = 5000;
const unsigned long INTERVAL_SERIAL_JSON = 200;  //  5 Hz → Mini PC dashboard

// ---------------------------------------------------------------------------
//  Forward declarations
// ---------------------------------------------------------------------------
void  pollBMS();
void  processCanBMS();
void  processCanOUT();
void  parseBMSFrame(uint32_t id, uint8_t len, uint8_t *d);
void  broadcastDiagnostics();
void  handleOBD2(uint8_t service, uint8_t pid);
void  readACS();
float calibrateSensor(int pin);
float readCurrent(int pin, float zeroVal);
void  printDebug();
void  printSerialJSON();

// ---------------------------------------------------------------------------
//  SETUP
// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  Serial.println(F("=== UTV ECU Booting ==="));

  if (canBMS.begin(MCP_ANY, CAN_500KBPS, MCP_8MHZ) == CAN_OK) {
    canBMS.setMode(MCP_NORMAL);
    Serial.println(F("[canBMS] OK  500 Kbps"));
  } else {
    Serial.println(F("[canBMS] FAILED — check wiring / crystal"));
  }

  if (canOUT.begin(MCP_ANY, CAN_500KBPS, MCP_8MHZ) == CAN_OK) {
    canOUT.setMode(MCP_NORMAL);
    Serial.println(F("[canOUT] OK  500 Kbps"));
  } else {
    Serial.println(F("[canOUT] FAILED — check wiring / crystal"));
  }

  attachInterrupt(digitalPinToInterrupt(BMS_INT), ISR_bms, FALLING);
  attachInterrupt(digitalPinToInterrupt(OUT_INT), ISR_out, FALLING);

  Serial.println(F("Calibrating ACS712 (keep all circuits OFF)..."));
  for (int i = 0; i < 6; i++) {
    acsZero[i] = calibrateSensor(ACS_PIN[i]);
    Serial.print(F("  ch")); Serial.print(i);
    Serial.print(F(" zero=")); Serial.println(acsZero[i], 1);
  }
  Serial.println(F("Calibration done."));

  memset(&bms, 0, sizeof(bms));
  memset(&acs, 0, sizeof(acs));
  for (int i = 0; i < 4; i++) bms.temp[i] = -128;

  Serial.println(F("=== ECU Ready ==="));
}

// ---------------------------------------------------------------------------
//  LOOP
// ---------------------------------------------------------------------------
void loop() {
  unsigned long now = millis();

  if (now - t_bmsPoll >= INTERVAL_BMS_POLL) {
    t_bmsPoll = now;
    pollBMS();
  }

  if (bms_pending) { bms_pending = false; processCanBMS(); }
  if (out_pending) { out_pending = false; processCanOUT(); }

  if (now - t_acsRead >= INTERVAL_ACS_READ) {
    t_acsRead = now;
    readACS();
  }

  if (now - t_diagTx >= INTERVAL_DIAG_TX) {
    t_diagTx = now;
    broadcastDiagnostics();
  }

  if (now - t_debug >= INTERVAL_DEBUG) {
    t_debug = now;
    printDebug();
  }

  if (now - t_serialJSON >= INTERVAL_SERIAL_JSON) {
    t_serialJSON = now;
    printSerialJSON();
  }
}

// ---------------------------------------------------------------------------
//  BMS POLLING  — two frames every 1 s
// ---------------------------------------------------------------------------
void pollBMS() {
  uint8_t keepAlive[8] = {0xC2, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};
  canBMS.sendMsgBuf(0x35C, 0, 8, keepAlive);         // STD frame

  uint8_t dumpCmd[8] = {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};
  canBMS.sendMsgBuf(0x0400FF80, 1, 8, dumpCmd);       // EXT frame (flag=1)
}

// ---------------------------------------------------------------------------
//  CAN RECEIVE — BMS bus
// ---------------------------------------------------------------------------
void processCanBMS() {
  uint8_t ext, len, data[8];
  uint32_t id;

  while (canBMS.checkReceive() == CAN_MSGAVAIL) {
    canBMS.readMsgBuf(&id, &ext, &len, data);
    if (ext == 1) parseBMSFrame(id, len, data);
  }
}

// ---------------------------------------------------------------------------
//  CAN RECEIVE — Output bus  (OBD-II / scan tool requests)
// ---------------------------------------------------------------------------
void processCanOUT() {
  uint8_t ext, len, data[8];
  uint32_t id;

  while (canOUT.checkReceive() == CAN_MSGAVAIL) {
    canOUT.readMsgBuf(&id, &ext, &len, data);
    if ((id == 0x7DF || id == 0x7E0) && len >= 3)
      handleOBD2(data[1], data[2]);
  }
}

// ---------------------------------------------------------------------------
//  BMS FRAME PARSER
// ---------------------------------------------------------------------------
void parseBMSFrame(uint32_t id, uint8_t len, uint8_t *d) {
  switch (id) {

    case 0x04028001:   // Pack V / I / SOC
      if (len < 6) break;
      bms.packV = ((uint16_t)(d[0] << 8) | d[1]) / 10.0f;
      bms.packA = ((int32_t)((d[2] << 8) | d[3]) - 30000) / 10.0f;
      bms.soc   = ((uint16_t)(d[4] << 8) | d[5]) / 10.0f;
      bms.valid = true;
      break;

    case 0x04008001: { // Cell voltages (frame 1–7, 3 cells each)
      uint8_t fn = d[0];
      if (fn < 1 || fn > 7) break;
      int base = (fn - 1) * 3;
      for (int i = 0; i < 3; i++) {
        int idx = base + i;
        if (idx < 19)
          bms.cellV[idx] = ((uint16_t)(d[1 + i*2] << 8) | d[2 + i*2]) / 1000.0f;
      }
      break;
    }

    case 0x04018001: { // Temperatures
      if (d[0] != 1) break;
      for (int i = 0; i < 4 && (i+1) < len; i++)
        bms.temp[i] = (d[1+i] == 0xFF) ? -128 : (int8_t)(d[1+i] - 40);
      break;
    }

    case 0x04048001:   // Cell min / max
      if (len < 6) break;
      bms.cellMax    = ((uint16_t)(d[0] << 8) | d[1]) / 1000.0f;
      bms.cellMaxIdx = d[2];
      bms.cellMin    = ((uint16_t)(d[3] << 8) | d[4]) / 1000.0f;
      bms.cellMinIdx = d[5];
      break;

    case 0x04068001:   // MOS state
      if (len < 3) break;
      bms.chargeMOS    = d[1];
      bms.dischargeMOS = d[2];
      break;

    case 0x04088001:   // Pack info
      if (len < 6) break;
      bms.cellCount  = d[0];
      bms.ntcCount   = d[1];
      bms.cycleCount = ((uint16_t)(d[4] << 8) | d[5]);
      break;

    case 0x040E8001: { // Faults
      if (d[0] != 1) break;
      bms.faultPresent = false;
      for (int i = 0; i < 7 && (i+1) < (int)len; i++) {
        bms.faultBytes[i] = d[1+i];
        if (d[1+i]) bms.faultPresent = true;
      }
      break;
    }
  }
}

// ---------------------------------------------------------------------------
//  DIAGNOSTICS BROADCAST  (canOUT — 10 Hz)
//
//  ID    Content
//  0x100 Pack V / I / SOC / MOS state
//  0x101 Cell voltages 1–3
//  0x102 Cell voltages 4–6
//  0x103 Cell voltages 7–9
//  0x104 Cell voltages 10–12
//  0x105 Cell voltages 13–15
//  0x106 Cell voltages 16–18
//  0x107 Cell voltage 19
//  0x110 Temperatures (4 NTC) + cell min/max voltages
//  0x120 BMS status — faults / cycles / cell count / min/max index
//  0x130 ACS ch0–3 (Turn, Hazard, Brake, Reverse)
//  0x131 ACS ch4–5 (Horn, Headlight)
// ---------------------------------------------------------------------------
void broadcastDiagnostics() {
  uint8_t buf[8];

  if (bms.valid) {

    // 0x100 — pack overview
    uint16_t pv = (uint16_t)(bms.packV * 10);
    uint16_t pa = (uint16_t)((bms.packA + 3000.0f) * 10);
    uint16_t ps = (uint16_t)(bms.soc * 10);
    buf[0]=pv>>8; buf[1]=pv&0xFF;
    buf[2]=pa>>8; buf[3]=pa&0xFF;
    buf[4]=ps>>8; buf[5]=ps&0xFF;
    buf[6]=bms.chargeMOS; buf[7]=bms.dischargeMOS;
    canOUT.sendMsgBuf(0x100, 0, 8, buf);

    // 0x101–0x107 — cell voltages
    for (int frame = 0; frame < 7; frame++) {
      for (int i = 0; i < 3; i++) {
        int idx = frame * 3 + i;
        uint16_t cv = (idx < 19) ? (uint16_t)(bms.cellV[idx] * 1000) : 0;
        buf[i*2]   = cv >> 8;
        buf[i*2+1] = cv & 0xFF;
      }
      buf[6] = 0; buf[7] = 0;
      canOUT.sendMsgBuf(0x101 + frame, 0, 8, buf);
    }

    // 0x110 — temperatures + cell spread
    for (int i = 0; i < 4; i++)
      buf[i] = (bms.temp[i] == -128) ? 0xFF : (uint8_t)(bms.temp[i] + 40);
    uint16_t mn = (uint16_t)(bms.cellMin * 1000);
    uint16_t mx = (uint16_t)(bms.cellMax * 1000);
    buf[4]=mn>>8; buf[5]=mn&0xFF;
    buf[6]=mx>>8; buf[7]=mx&0xFF;
    canOUT.sendMsgBuf(0x110, 0, 8, buf);

    // 0x120 — BMS status
    buf[0] = bms.faultPresent ? 1 : 0;
    buf[1] = bms.cellCount;
    buf[2] = bms.ntcCount;
    buf[3] = bms.cellMinIdx;
    buf[4] = bms.cellMaxIdx;
    buf[5] = bms.cycleCount >> 8;
    buf[6] = bms.cycleCount & 0xFF;
    buf[7] = 0;
    canOUT.sendMsgBuf(0x120, 0, 8, buf);
  }

  // 0x130 — ACS ch0–3
  for (int i = 0; i < 4; i++) {
    uint16_t r = (uint16_t)(acs.current[i] * 100);
    buf[i*2]   = r >> 8;
    buf[i*2+1] = r & 0xFF;
  }
  canOUT.sendMsgBuf(0x130, 0, 8, buf);

  // 0x131 — ACS ch4–5
  uint16_t r4 = (uint16_t)(acs.current[4] * 100);
  uint16_t r5 = (uint16_t)(acs.current[5] * 100);
  buf[0]=r4>>8; buf[1]=r4&0xFF;
  buf[2]=r5>>8; buf[3]=r5&0xFF;
  buf[4]=buf[5]=buf[6]=buf[7]=0;
  canOUT.sendMsgBuf(0x131, 0, 8, buf);
}

// ---------------------------------------------------------------------------
//  OBD-II PID RESPONDER  (Mode 0x22 — manufacturer-specific)
//
//  PID  Content
//  0x01 Pack voltage       uint16 ×10    V
//  0x02 SOC                uint8         %
//  0x03 Pack current       uint16 +3000 ×10  A
//  0x04 Cell min voltage   uint16 ×1000  V
//  0x05 Cell max voltage   uint16 ×1000  V
//  0x10 ACS Turn Signal    uint16 ×100   A
//  0x11 ACS Hazard         uint16 ×100   A
//  0x12 ACS Brake          uint16 ×100   A
//  0x13 ACS Reverse        uint16 ×100   A
//  0x14 ACS Horn           uint16 ×100   A
//  0x15 ACS Headlight      uint16 ×100   A
// ---------------------------------------------------------------------------
void handleOBD2(uint8_t service, uint8_t pid) {
  uint8_t resp[8] = {0x03, 0x7F, service, 0x12, 0, 0, 0, 0};

  if (service == 0x22) {
    resp[1] = 0x62;
    resp[2] = pid;

    switch (pid) {
      case 0x01: { uint16_t v=(uint16_t)(bms.packV*10);
                   resp[0]=0x05; resp[3]=v>>8; resp[4]=v&0xFF; break; }
      case 0x02:   resp[0]=0x04; resp[3]=(uint8_t)(bms.soc); break;
      case 0x03: { uint16_t c=(uint16_t)((bms.packA+3000.0f)*10);
                   resp[0]=0x05; resp[3]=c>>8; resp[4]=c&0xFF; break; }
      case 0x04: { uint16_t v=(uint16_t)(bms.cellMin*1000);
                   resp[0]=0x05; resp[3]=v>>8; resp[4]=v&0xFF; break; }
      case 0x05: { uint16_t v=(uint16_t)(bms.cellMax*1000);
                   resp[0]=0x05; resp[3]=v>>8; resp[4]=v&0xFF; break; }
      case 0x10: case 0x11: case 0x12:
      case 0x13: case 0x14: case 0x15: {
        uint16_t a=(uint16_t)(acs.current[pid-0x10]*100);
        resp[0]=0x05; resp[3]=a>>8; resp[4]=a&0xFF; break;
      }
      default:
        resp[0]=0x03; resp[1]=0x7F; resp[2]=service; resp[3]=0x12; break;
    }
  }
  canOUT.sendMsgBuf(0x7E8, 0, 8, resp);
}

// ---------------------------------------------------------------------------
//  ACS712
// ---------------------------------------------------------------------------
void readACS() {
  for (int i = 0; i < 6; i++)
    acs.current[i] = readCurrent(ACS_PIN[i], acsZero[i]);
}

float calibrateSensor(int pin) {
  long total = 0;
  for (int i = 0; i < 500; i++) { total += analogRead(pin); delay(2); }
  return total / 500.0f;
}

float readCurrent(int pin, float zeroVal) {
  long total = 0;
  for (int i = 0; i < 200; i++) total += analogRead(pin);
  float vDiff   = (total / 200.0f - zeroVal) * (5.0f / 1023.0f);
  float current = fabsf(vDiff / ACS_SENS);
  return (current < ACS_NOISE_FLOOR) ? 0.0f : current;
}

// ---------------------------------------------------------------------------
//  JSON SERIAL OUTPUT  (5 Hz → Mini PC dashboard via USB)
//  Single compact line per frame — parsed by dashboard.py
//  Fields: pv=packV, pa=packA, soc, pw=powerW, t0-t3=temps,
//          cmin/cmax=cell min/max V, flt=faultPresent,
//          cv=19 cell voltages array, ac=6 ACS currents array
// ---------------------------------------------------------------------------
void printSerialJSON() {
  Serial.print(F("{\"pv\":"));   Serial.print(bms.packV, 2);
  Serial.print(F(",\"pa\":"));   Serial.print(bms.packA, 2);
  Serial.print(F(",\"soc\":"));  Serial.print(bms.soc, 1);
  Serial.print(F(",\"pw\":"));   Serial.print(bms.packV * fabsf(bms.packA), 1);
  Serial.print(F(",\"t0\":"));   Serial.print(bms.temp[0] == -128 ?  -99 : (int)bms.temp[0]);
  Serial.print(F(",\"t1\":"));   Serial.print(bms.temp[1] == -128 ?  -99 : (int)bms.temp[1]);
  Serial.print(F(",\"t2\":"));   Serial.print(bms.temp[2] == -128 ?  -99 : (int)bms.temp[2]);
  Serial.print(F(",\"t3\":"));   Serial.print(bms.temp[3] == -128 ?  -99 : (int)bms.temp[3]);
  Serial.print(F(",\"cmin\":")); Serial.print(bms.cellMin, 3);
  Serial.print(F(",\"cmax\":")); Serial.print(bms.cellMax, 3);
  Serial.print(F(",\"cmni\":")); Serial.print(bms.cellMinIdx);
  Serial.print(F(",\"cmxi\":")); Serial.print(bms.cellMaxIdx);
  Serial.print(F(",\"cyc\":"));  Serial.print(bms.cycleCount);
  Serial.print(F(",\"mos\":"));  Serial.print(bms.chargeMOS);
  Serial.print(F(",\"flt\":"));  Serial.print(bms.faultPresent ? 1 : 0);
  Serial.print(F(",\"vld\":"));  Serial.print(bms.valid ? 1 : 0);

  // 19 cell voltages
  Serial.print(F(",\"cv\":["));
  for (int i = 0; i < 19; i++) {
    Serial.print(bms.cellV[i], 3);
    if (i < 18) Serial.print(',');
  }
  Serial.print(F("]"));

  // 6 ACS currents
  Serial.print(F(",\"ac\":["));
  for (int i = 0; i < 6; i++) {
    Serial.print(acs.current[i], 2);
    if (i < 5) Serial.print(',');
  }
  Serial.println(F("]}"));
}

// ---------------------------------------------------------------------------
//  DEBUG SERIAL  (every 5 s @ 115200 baud)
// ---------------------------------------------------------------------------
void printDebug() {
  Serial.println(F("--- ECU State ---"));

  if (bms.valid) {
    Serial.print(F("  Pack   : ")); Serial.print(bms.packV, 1);
    Serial.print(F("V  "));        Serial.print(bms.packA, 1);
    Serial.print(F("A  SOC="));    Serial.print(bms.soc, 1); Serial.println(F("%"));

    Serial.print(F("  Cells  : min=")); Serial.print(bms.cellMin, 3);
    Serial.print(F("V[#"));             Serial.print(bms.cellMinIdx);
    Serial.print(F("]  max="));         Serial.print(bms.cellMax, 3);
    Serial.print(F("V[#"));             Serial.print(bms.cellMaxIdx); Serial.println(F("]"));

    Serial.print(F("  Temps  :"));
    for (int i = 0; i < 4; i++) {
      if (bms.temp[i] == -128) Serial.print(F("  --"));
      else { Serial.print(F("  ")); Serial.print(bms.temp[i]); Serial.print(F("C")); }
    }
    Serial.println();
    Serial.print(F("  Cycles : ")); Serial.println(bms.cycleCount);
    Serial.print(F("  Faults : ")); Serial.println(bms.faultPresent ? F("YES") : F("none"));
  } else {
    Serial.println(F("  BMS    : no data yet"));
  }

  const char* ch[6] = {"Rev","Brake","Head","Hazard","Turn","Horn"};
  Serial.print(F("  ACS    :"));
  for (int i = 0; i < 6; i++) {
    Serial.print(F("  ")); Serial.print(ch[i]);
    Serial.print(F("=")); Serial.print(acs.current[i], 2); Serial.print(F("A"));
  }
  Serial.println();
  Serial.println(F("-----------------"));
}
