# ==============================================================================
# Migración Ambar/UCA — Aplica BASE completa (según tipo) + Interfaces nuevas
# - Pide hostname AMBAR (para POE y AMBAR_T) y hostname UCA (para UCA).
# - Para cada switch destino:
#     1) Inserta la PLANTILLA BASE COMPLETA del tipo (AMBAR/UCA/VIDEO), forzando 'hostname <pedido>'.
#     2) Añade interfaces nuevas clonadas del running de origen (sin 'sticky').
#     3) Replica puertos trunk seleccionados al final del stack (sin port-channels) en AMBAR.
#     4) Para VIDEO respeta el orden original de las interfaces y no aplica filtros de actividad.
# ==============================================================================

import os
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from typing import Iterable, List, Dict, Tuple, Optional, Callable, Union, Set, Any

import pandas as pd


# --- Parámetros de switches destino (nombres internos para el mapeo) ---
NEW_SWITCH_WIFI_NAME    = "SW-NUEVO-UXM-01"   # Miembro con WIFI (TwoGig/TenGig)
NEW_SWITCH_POE_NAME     = "SW-NUEVO-POE-01"   # Miembros POE sin WIFI

NEW_SWITCH_AMBAR_T_NAME = "SW-NUEVO-AMBAR-T-01"
NEW_IF_AMBAR_T_PREFIX   = "GigabitEthernet1/0/"

NEW_SWITCH_UCA_T_NAME   = "SW-NUEVO-UCA-T-01"
NEW_IF_UCA_T_PREFIX     = "GigabitEthernet1/0/"

NEW_SWITCH_VIDEO_NAME   = "SW-NUEVO-VIDEO-01"
NEW_IF_VIDEO_PREFIX     = "GigabitEthernet1/0/"

# --- Valores por defecto ---
DEFAULT_SNMP_CONTACT = "dmanau@aena.es"

# --- Conjuntos de VLAN objetivo ---
WIFI_VLANS = {"360", "361"}
VOIP_VLANS = {"1211", "1223", "1225", "1226", "1227", "1228", "1229"}
MANDATORY_POE_VLANS = {
    "66",
    "114",
    "143",
    "144",
    "218",
    "219",
    "287",
    "325",
    "330",
    "331",
    "360",
    "361",
    "380",
    "382",
    "383",
    "384",
    "385",
    "386",
    "388",
    "389",
    "390",
    "391",
    "393",
    "394",
    "410",
    "462",
    "467",
    "469",
    "1225",
    "1226",
    "1227",
    "1228",
    "1229",
    "3500",
    "357",
}


# --- Clasificación de elementos que requieren POE ---
def _item_requires_poe_support(item: Dict[str, Any]) -> bool:
    """Return True if the given mapping entry must stay on PoE hardware."""

    vlan = str(item.get("vlan") or "").strip()
    if vlan and vlan in MANDATORY_POE_VLANS:
        return True

    voice_vlan = item.get("voice_vlan")
    if voice_vlan and str(voice_vlan) in MANDATORY_POE_VLANS:
        return True

    allowed_vlans = item.get("allowed_vlans") or []
    for allowed in allowed_vlans:
        vlan_value = str(allowed).strip()
        if vlan_value and vlan_value in MANDATORY_POE_VLANS:
            return True

    return False

UCA_VLANS  = {
    "3", "29", "134", "137", "138", "141", "142", "451", "453", "454", "455",
    "623", "2204", "2205", "2206", "2207", "2208", "2209", "2210", "2211", "2212",
    "2215", "2216", "2217", "2218", "2219", "2221", "2222", "2230", "2300"
}
IGNORED_VLANS = {"606"}
# --- Marcado de VLANs en Excel ---
#   - Rojo: 13,324,325,286,287,330,331
#   - Amarillo: 168,169,171,175–187
RED_VLANS = {"13", "324", "325", "286", "287", "330", "331"}
YELLOW_VLANS = {
    "168", "169", "171", "175", "176", "177", "178", "179", "180", "181",
    "182", "183", "184", "185", "186", "187"
}

# --- Límites de actividad ---
INACTIVITY_THRESHOLD_SECONDS = 13 * 7 * 24 * 3600  # 13 semanas

# --- Catálogo de VLANs conocidas (para declarar solo las usadas por el stack) ---
_VLAN_NAME_DATA = """
2 AO-Aena
3 AO-UCA1-Red_Impar
4 FTTH-Optima Facility
5 CIAS-ADSL_SITA
6 CIAS-AEA-(Groundforce)
7 FTTH-Capiedal
8 TPV-Aldeasa
9 TPV-Vidal-Vidal
10 CIAS-Emirates
11 CIAS-Swissport Handling-FTTH
12 CIAS-Lufthansa
13 AO-Policia_1
14 AO-DGGC-sitel
15 CIAS-Hangar-ICGC-FTTH
16 CIAS-British-Airways
17 CIAS-TAP_SITA
18 CIAS-AirNostrum
19 CIAS-Cargolux
20 AO-Monbus
21 TPV-Areas
22 AO-DGGC
23 AO-Video_Conferencia
24 CIAS-GALP_ADSL
25 AO-SITA
26 Reservado NGN
27 CIAS-SwissTerminal
28 CIAS-EmiratesCargo
29 AO-UCA2-Red_Par
30 CIAS-Continental
31 CIAS-Acciona
33 CIAS-Valoriza(optima Facility)
34 TPV-LagardereTR
35 TPV-Cristal
36 CIAS-EuroDivisas
37 Toip Vueling
38 CIAS-SIXTFTTH
39 AO-Mnto.Scadas
40 CIAS-BRS-PRO
41 AO-Parking-Vip
42 CIAS-AGA
43 AO-Siketing
44 AO-SEGURIDAD
45 CIAS-EASYJET-WAN_(ARINC)
46 AO-Crtl Inst
47 CIAS-SwissCargo
48 TPV-Excess Baggage
49 CIAS-Panasonic-FTTH
50 AO-Cajeros_Cobro
51 TPV-Whsmith
52 AO-MWC2016
53 AO-MossosToIP1
54 TPV-Tous
55 AO-MossosToIP2
56 AO-IPV-Migration. BCN1200,1202,2200,2202
57 AO- Live Migration HyperV  BCN1330,2330
58 CIAS-Turkish-Airlines_ADSL
59 CIAS-NorwegianCCAA
60 TPV-PansFood
61 TPV-Hertz T1
62 TPV-La Tormenta Perfecta
63 TPV-Hertz T2
64 CIAS-AviancaAPH
65 CIAS-Vueling
66 AO-Taxis-VTc
67 AO-Barix_Sate
68 CIAS-EGYPTAIR
69 AO-SICAM
70 CIAS-NWGWIFI
71 AO-ClientesADSL
72 AO-VTC
73 TPV-Superskunk
74 TPV-BOBOLI
75 CIAS-Gestair-FTTH
76 AO-Qsystem y QMATIC
77 AO-PilotoTaxis
78 AO-AMBARCPISCAP
79 CIAS-US_Airways
80 TPV-Natura
81 TPV-DOLCEMANIA(Neucroissant)
82 TPV-Parfois
83 AO-Eq_Inspec_T2
84 AO-GCivilAdsl
85 TPV-DMZ-Pans
86 AO-MegafoniaT2_1
87 AO-MegafoniaT2_2
88 AO-PanelesDinamicos
89 AO-Enlace tráfico BCN-FWBCNCPDeam
90 TPV-EuroDivisas
91 AO-Aena-SIRAM
92 TPV-Capiedal-Parafarmacia
93 TPV-Alehop (Clave Denia)
94 CIAS-DMZAcciona
95 CIAS-Aerospace-FTTH
96 AO-Gestion_Cola
97 AO-Aduanas
98 AO-SirBCN
99 AO-Sonometros_WIMAX
100 AO-DMZFW
101 AO-DMZINTERNET
102 CIAS-Norwegian
103 CIAS-TRABLISA
104 CIAS-Alitalia
105 TPV-Lolly
106 TPV-Autogrill
107 TPV-Brownie
108 CIAS-GAMA
109 AO-ATIS
111 TPV-Truestar
112 TPV-Lagardere
113 AO-Xovis_Servers
114 AO-Xovis_Devices1
115 CIAS-Eurodivisas2
116 CIAS-Arinc
117 TPV-Premiere
118 TPV-ChocolatFactory
119 TPV-Farmacia
120 AO-SAVIA
121 TPV-Buff
122 AO-EqTrazasSeg
123 AO-Test
124 TPV-PL2000 E tomas
125 TPV-EXCESS_BAGGAGE_T2
126 FTTH-SWIFTAIR
127 TPV-Burberry
128 AO-VehiclesElectrics
129 TPV-GroupSerhs
130 AO-icts
131 AO-Argos
132 AO-AudioMonitor
133 AO-PilotoCGA
134 AO-uca3
135 AO-CA_Aire
136 TPV-GroupSerhsWIFI
137 AO-UCA_SBD_T2
138 AO-UCA-5
139 Wifi-Mossos
140 CIAS-AirChina
141 AO-SIPAS_SVC2
142 AO-SIPAS_SVC3
143 AO-DispSerT1
144 AO-DispSerT2
145 FTTH-ALEHOP
146 TPV-Mango
147 AO-PLCsCLASA
148 CAPI-LUX
149 CIAS-AmericanAirlines
150 CIAS-AirFrance
151 CIAS-SwissTransporte
152 AO-Contadores T2
153 TPV-Capi-Lux
154 AO-TetraVortex
155 AO-Videoconf_Life
156 CIAS-Exterior_Plus
157 AO-CUSS
158 CIAS-LATAM
159 CIAS-Lufthansa_WAN
160 AO-CONCENTER-160
161 AO-CMAC-Vlan Span 1
162 AO-CMAC-VLAND_(GENESYS)
163 AO-CMAC-VLANF_(NICE)
164 AO-PMR
165 AO-Autoinformacion
166 TPV-Estanco
167 AO-GSA_Servers
168 AO-CCAA NTS
169 AO-Puestos_CCAA
170 TPV-Tuttifruti
171 AO-CCAA NEA
172 TPV-STAMP
173 Wifi-Team
174 CCTV aldeasa
175 AO-CCAA NTS 3
176 AO-CCAA NTS 4
177 AO-CCAA NTS 5
178 AO-CCAA NTS 6
179 AO-CCAA NTS 7
180 AO-CCAA NTS 8
181 AO-CCAA NTS 9
182 AO-CCAA NTS 10
183 AO-CCAA NTS 11
184 AO-CCAA NTS 12
185 AO-CCAA NTS 2
186 AO-CCAA NTS 13
187 AO-CCAA NEA 2
188 AO-Clientes
189 Cisco live MWC
190 AO-User_TIC
191 AO-User_AenaBT
192 AO-User_Aena
193 AO-User_Externos
194 AO-User_ESIA
195 AO_Proselec
196 AO-Operadores_CPD
197 AO-Usuarios_Aena_T2
198 AO-Operadores_CGA
199 AO-User_AenaBT2
200 AO-TVNetIP
201 Replica Cabinas
202 AO-Elecnor_SCADAS
203 TPV-DMZ-Areas
204 AO-VTH-T2
205 CIAS-Sagital
206 AO-Salas VIP
207 AO_Tecosa_ADM_SAMD
208 Lolacasademunt
209 TPV-TED BAKER
210 AO-AENA DN
211 AO-AENA_SCCM
212 AO-AENA DS
213 TPV-HugoBoss ?????
214 AO-AENA RESTO TERMINAL
215 FTTH1 MOSSOS
216 FTTH2 MOSSOS
217 AO-Usuarios Win7
218 AO-Xovis_Devices2
219 AO-Xovis_Devices3
220 AO-AENA_Impresoras
221 AO-AENA PRINT DS
222 AO-AENA PRINT RESTO TERMINAL
223 AO-AENA_Print_Ricoh
224 CIAS-IhandlinG_Rampa
225 AO-AENA_KVM1
226 AO-AENA_KVM2
227 TPV-BurberryTheatre
228 CIAS-IhandlinG_Ventas
229 CIAS-Iberia_ToIP
230 CIAS-LUFTHANSA_ToIP
231 CIAS-Iberia_Oficinas
232 CIAS-Iberia_UCA
233 AO-NTP
234 AO-INMETEO
235 AO-ControlLuminico
236 AO-RTU_T2
237 AO-SiketingRSPAN
238 AO-IberiaWifi
239 AO-TempFira
240 CIAS-EASPK
241 TPV-ZaraT1
242 FTTH-JAS
243 TPV-LaMallorquina
244 TPV-Repsol
245 TPV-SSP
246 TPV-JDSports
247 TPV-McDonalds_Sky
248 TPV-Barça
249 WIFI Brownie
251 TPV-SEPALEMEME(DESIGUAL)
252 TPV-TravelRetail services
253 AO-Mossos
254 FTTH-Cathay
255 TPV-Trade Center
256 CIA-TradeCenter_ToIP_Internet
257 CIAS-EasyJet-Oficinas
258 CIAS-EasyJet-Wifi
259 CIAS-Aegean Airlines
260 CIAS-ELAL-WiFi
261 AO-Scafis-gunebo
262 AO-Cuss_T2
263 AO-Cuss_T1
264 CIAS-Gestion_SBD_BBC
265 CIAS-Gestion_SBD_DSG
266 CIAS-SGMT
267 AO-SGESER
268 CIAS-DeltaOficinas
269 CIAS-NorwegianNIA
270 TPV-Tutifrutti_Modulo_U
271 Evento_BMW
272 Tutti-Frutti_T1
273 CIAS-Iberojet
274 TPV-McDonalds_Int
275 AO-Infinity
276 CIAS-AirAlgerie_SITA
277 AO-DENEVA
278 CIAS-Mencies2
279 volvo_evento_1
280 volvo_evento_2
281 CIAS-AegeanAirlines_ToIP
282 TPV-SunglassHut
283 AO_Tecosa_RX
284 CIAS-Soltour
285 TPV-Valerie
286 AO-POLICIA_2
287 AO-POLICIA_3
288 CIAS-Mencies_Aviation
289 AO-Cobro_Parking
290 CIAS-ASIANA_APH
291 Wifi-Sigma
292 CIAS-QatarAirways
293 CIAS-DeltaVentaBilletes
294 AO-UsuarioVAP
295 AO-TetraSDR
296 AO-TetraBackup
297 AO-CENTetra
298 CIAS-JET2
299 WIMAX
300 WIFI-Servidores
301 WIFI-LWAPP1
302 WIFI-LWAPP2
303 WIFI-ADSL
304 AO-FT1
305 AO-FT2
306 AO-FT3
307 AO-FT4
308 AO-FT5
309 RSPAN-MiTTEL
310 AO-DMZENA
311 AO-IDCCCAA
312 AO-Servers-NETID
313 AO_CUSS_AVA
314 AO_Clientes_AVA
315 AO_Servidores_AVA
316 AO_Gestion_Guardian
317 AO_PIPRA_HHAA
318 AO-Server_Lorawan_IT
319 CIAS-VOLOTEA
320 CIAS-AZULHANDLINGWIFI
321 AO-EqCemant_X80
322 CIAS-FTTH-FOTOVOLTAICA
323 CIAS-WIFI-LATAM
324 AO-CNP_Datos
325 AO-CNP_ABC
326 AO-CNP_Imagenes
327 AO-CNP_Filtros_PCM_2
328 AO-CNP_EES_Quioscos1
329 AO-CNP_EES_Quioscos2
330 AO-CNP_EES_PCAs-Tablets
331 AO-CNP_EES_ABC
332 AO-CNP_Sitel
333 AO-EDS_Servidores
334 AO_EDS_Remote
335 AO_EDS-Campo
336 AO_ATRS_Servidores
337 AO_ATRS_Scadas
338 AO_ATRS_Monitorizacion
339 AO-ATRS_Scadas
340 CIAS-Failover_SITA
341 WIFI-Control Datos
342 WIFI-Service WISM
343 WIFI-Service WISM
344 Vlan_1_WHSmith
345 Vlan_2_WHSmith
346 Vlan_3_WHSmith
347 Vlan_4_WHSmith_HA
348 WIFI-Datos1-T1_Singual_Roca_globos
349 WIFI-Aena-T1
350 WIFI-Aena-T2
351 WIFI-Aena-Campus
352 WIFI_Cortesia_T1
353 WIFI_Cortesia_T2
354 WIFI-Datos3-T2
355 WIFI-Datos4-T2
356 CIAS-WIFIWDF
357 AO-AntenasLoraWan
358 CIAS-EXCESS_BAGGAGE
360 WIFI-APs_T1
361 WIFI-APs_T2
362 WIFI-Redundancia_WLC_T1
363 WIFI-Redundancia_WLC_T2
364 FTTH-Fernatrans_Cargo
365 FTTH-Aviaparner
366 Dufry_digital
367 CIAS-LEVEL
368 CIAS-EURPOCAR_ProvFTTH_T2
369 CIAS-Vueling-Wifi_Firmas
370 Wifi-CarritosT1
371 AO-Carritos_EMV
372 AO-SRECVEHI_T1
373 AO-SRECVEHI_T2
374 Wifi-Barça
375 Wifi-Gatelink
376 Wifi-Gatelink822
378 Wifi-SITASEC
379 WIFI-SITA_APHw_MGMT
380 SCAP-T2
381 SCAP-Servidores
382 SCAP-Control
383 SCAP-Video-Audio1
384 SCAP-Campo
385 SCAP-Video-Audio2
386 PK_Larga_Estancia
387 PK-ViaT
388 PK_Express_VialesPares
389 PK_Express_VialesImpares
390 PK_Express_Cajeros
391 PK_T2_VIAL_IMPAR
392 PK_DMZ_UTEAS
393 PK_InterfSIP_UTEAS
394 BCN_INTERFONOS
395 BCN_Dispositivos_PK
400 AO-Macrolan_Aena
401 AO-SITA-SATE
402 Libre_RE DAN
403 AO-Ges_Switch_Redan
404 AO-Resina
405 AO-Redan
406 AO-Redan
408 AO-Gestion_Radio_Enlace
410 AO-NTP_T1
411 AO-RFICHAR_T1
412 AO-DMZSCAFISDGGC
413 AO-CBTH_T2
414 AO-Rdigital_T2
415 AO-400Hz_T2
420 AO-Pruebas_Argos
428 AO-AnilloClimaIntermodal
430 AO-ASC-WAN10
431 AO-ASC-WAN1
432 AO-ASC-WAN2
433 AO-ASC-WAN3
434 AO-ASC-WAN4
435 AO-ASC-WAN5
436 AO-ASC-WAN6
437 AO-ASC-WAN7
438 AO-ASC-WAN8
439 AO-ASC-WAN9
440 Free_Wifi_T1
441 Free_Wifi_T2
442 Free_Wifi_VIP_T1
443 Free_Wifi_VIP_T2
444 WIFI_Eventos_Vodafone
445 AO-EventosInternet
446 AO-ElectroSCIT2
447 AO-HB_SMP_en_Hiper_V
448 AO-HB_SIGMA_en_Hiper_V
449 AO-HB_SCI_en_HiperV
450 AO-INFECTADOS
451 AO-SIPAV6-T2-Gen1
452 CCTV_Termica
453 AO-SIPAV6-T2-Gen2
454 AO-SIPAV6-T2-Impar
455 AO-SIPAV6-T2-Par
456 AO-ULISES
457 AO-EqCemantT2
458 AO-IntfSIPPIPRA
459 AO-IOKEEN
460 AO-APNbcnUser
461 AO-APNbcnDMZ
462 AO-InterfoniaSIP
463 AO-ReportColas
464 AO-BIOPASS
465 CIAS-SKYTANKING_Datos
466 CIAS-SKYTANKING_Toip
467 AO-InterfoniaPK
468 AO-ElecnorMtoScadas
469 AO-XovisDevices4
470 AO-RemoteScreenig
471 AO-ATRS
472 AO-SRV-RemoteScreenig
473 FTTH-ExteriorPlus
474 AO-Backup-Cloud
475 AO-Cabina-Cloud
476 CIAS-Vueling-Sala201
477 CIAS-Vueling-Wifi_Crew
478 CIAS-Vueling_Almacen
479 TPV-CarrefourMP
480 CIA-Eurest_Ftth
481 CIA-FCL-Backup
482 TPV-Pikolinos
500 AO-vSAN
501 AO-VmWare_Vsan
502 AO-VLAN_VMWARE_Fault_Tolerance
503 AO-VLAN_VMWARE_Vmotion
504 AO-vSAN_SMP
505 AO-HCI_SQL_AlwaysON_smp
506 AO-HCI_SQL_AlwaysON_genetec
507 AO-HCI_HB_SCI
508 AO-HCI_HB_SCE
509 AO-HCI_HB_SIGMA
510 AO-CCTV_AMBAR
511 AO-CCTV_GUIAST2
512 AO-CCTV_T2_Cabina
513 AO-CCTV_T2_Ext
514 AO-CCTV_T2_Roto
520 AO-TVSENALETICA
521 AO-HB_SAN_SCADA_SMP1
522 AO-HB_SAN_SCADA_SMP2
523 AO-HB_SAN_SCADA_SCE
524 AO-HB_SAN_SCADA_OT
525 AO-HB_SAN_SCADA_SMPPRE
555 CIA-IBERIA_UCA
556 AO-CNP_Axon/Taser
557 AO-CNP_Img_CCTV
558 AO-CNP_Videoconf
629 AO-REVELA
880 CIAS-AVOLTA_GAT
881 CIAS-AVOLTA_POS
882 CIAS-AVOLTA_POM
883 CIAS-AVOLTA_WPT
884 CIAS-AVOLTA_POK
885 CIAS-AVOLTA_MED
888 AO-UNION_RED_CENTRALITAS_MXONE
911 Reservado_SDWAN
950 CIAS-SINGAPORE
951 CIAS-AIRFRANCE_CARGO
961 CIAS-UnitedAirlinesSDWAN
1100 AO-MTS_PRIMARY
1111 AO-MTS_SECONDARY
1200 Reservado_Telefonia_IP
1201 Servidores_Telefonia_IP
1202 CLAN_y_Media_Processor
1203 Reservado_Telefonia_IP
1204 Reservado_Telefonia_IP
1205 AO-BCN_MXONE
1206 Reservado_Telefonia_IP
1207 Reservado_Telefonia_IP
1208 Reservado_Telefonia_IP
1210 Telefonos_IP_2
1211 Telefonos_IP_3
1212 Telefonos_IP_4
1213 Telefonos_IP_CMAC
1214 Reservado_Telefonos_IP
1215 Interfonias_SIP
1216 Reservado_Telefonos_IP
1217 Reservado_Telefonos_IP
1218 Reservado_Telefonos_IP
1219 Reservado_Telefonos_IP
1225 AO-MXONE1-ToIP
1226 AO-MXONE2-ToIP
1227 AO-MXONE3-ToIP
1228 AO-MXONE4-ToIP
1229 AO-MXONE5-ToIP
1235 AO-MXONE_TA
1236 AO-MXONE-MEGAFONIA
1237 AO-Servidores_ContactCenter
1238 Reservado_Telefonos_IP
1239 Reservado_Telefonos_IP
1245 Reservado_Telefonos_IP
1246 Reservado_Telefonos_IP
1247 AO-PK
1248 AO-CENAT
1249 Reservado_Telefonos_IP
1250 Megafonia-EN54-SIP
1251 Megafonia_AD01
1252 Megafonia_AD02
1253 Megafonia_AD03
1254 Megafonia_AD04
1255 Megafonia_AD05
1256 Megafonia_AD06
1257 Megafonia_AD07
1258 Megafonia_AD08
1259 Megafonia_AD09
1260 AO-RADS
1261 AO-EnlaceGS_MD110
1262 Pruebas_ToIP_1262
1263 Pruebas_ToIP_1263
1264 AO-RADCENAT
1265 AO-MEGA_DANTE
1313 AO-GESNAC
1333 Nuevo_WIFI_GUEST_T1
1334 Nuevo_WIFI_GUEST_T2
1444 Nuevo_WIFI_VIP_T1
1445 Nuevo_WIFI_VIP_T2
1800 VLAN-1800
2214 UCA14
2220 AO-SBDs_T1
2401 AO-ConsoleSite-PR
2501 AO-ConsoleSite-SC
2529 AO-Pipra_Bcn
3000 Reservado_SincronismoCoresAmbar
3001 Reservado_SDWAN
3100 AO-guias_atraque
3101 AO-Equip_Aeron_T2
3500 AO-GW_Lorawan_IT
"""

VLAN_NAME_MAP: Dict[str, str] = {}
for _raw in _VLAN_NAME_DATA.strip().splitlines():
    entry = _raw.strip()
    if not entry:
        continue
    parts = entry.split(None, 1)
    if not parts:
        continue
    vlan_id = parts[0]
    if not vlan_id.isdigit():
        continue
    vlan_name = parts[1].strip() if len(parts) > 1 else ""
    if vlan_name:
        VLAN_NAME_MAP[vlan_id] = vlan_name

VLAN_NAME_MAP.setdefault("623", "BCN_ACC_GESTION_AMBAR")

# --- Capacidad y reservas del switch POE ---
POE_MAX_PORTS          = 48
RESERVED_AFTER_WIFI    = 4
RESERVED_AFTER_VOIP    = 10
RESERVED_TAIL_FREE     = 10
POE_AUX_FINAL_PORTS    = 24   # último miembro de 24 si faltan <= 24
POE_MAX_STACK_MEMBERS  = 3    # WIFI + POE + AMBAR (máximo)
WIFI_TWOGIG_LIMIT      = 36

# --- Capacidad de switches adicionales ---
AMBAR_T_DEFAULT_PORTS = 24
AMBAR_T_LARGE_PORTS   = 48
AMBAR_T_RESERVED_TAIL = 10
UCA_DEFAULT_PORTS = 24
UCA_LARGE_PORTS   = 48
UCA_RESERVED_TAIL_FREE = 3

# --- Rutas por defecto de plantillas base ---
AMBAR_TEMPLATE_DEFAULT       = "AMBAR template actualizado v3.txt"
UCA_TEMPLATE_DEFAULT         = "UCA template actualizado v2.txt"
AMBAR_EXTRA_TEMPLATE_DEFAULT = "ambar_config_base_extra.txt"
UCA_EXTRA_TEMPLATE_DEFAULT   = "uca_config_base_extra.txt"

# --- Estado dinámico del mapeo POE ---
POE_MEMBER_METADATA: Dict[int, Dict[str, object]] = {}


def _poe_member_meta_by_name(sw_name: str) -> Optional[Dict[str, object]]:
    """Return the POE member metadata associated to ``sw_name`` if available."""

    for info in POE_MEMBER_METADATA.values():
        if str(info.get("sw_name")) == sw_name:
            return info
    return None


def _poe_display_label(
    sw_name: str,
    *,
    interfaces: Optional[Iterable[str]] = None,
    tags: Optional[Iterable[str]] = None,
    meta_by_name: Optional[Dict[str, Dict[str, object]]] = None,
) -> str:
    """Return the human friendly label for a POE stack member."""

    meta: Optional[Dict[str, object]] = None
    if meta_by_name is not None:
        meta = meta_by_name.get(sw_name)
    if meta is None:
        meta = _poe_member_meta_by_name(sw_name)

    capacity: Optional[int] = None
    member_type = ""

    if meta:
        raw_capacity = meta.get("capacity")
        try:
            capacity = int(raw_capacity) if raw_capacity is not None else None
        except (TypeError, ValueError):
            capacity = None
        raw_type = meta.get("type")
        if isinstance(raw_type, str):
            member_type = raw_type.lower()

    is_wifi = member_type == "wifi"
    is_data_only = member_type == "data"

    if capacity is None and interfaces:
        max_port = 0
        for ifname in interfaces:
            if isinstance(ifname, str):
                idx = _extract_if_index(ifname)
                if idx and idx > max_port:
                    max_port = idx
        if max_port:
            capacity = 24 if max_port <= 24 else 48

    if capacity is None:
        capacity = 48

    if not is_wifi and member_type != "data" and tags:
        for tag in tags:
            if isinstance(tag, str) and "ORIGIN=WIFI" in tag.upper():
                is_wifi = True
                break

    if is_wifi:
        suffix = "P-UXM"
    elif is_data_only:
        suffix = "T"
    else:
        suffix = "P"

    label_size = 24 if capacity <= 24 else 48
    return f"C9300-{label_size}{suffix}"


def _video_display_label(interfaces: Iterable[str]) -> str:
    """Return the display label for VIDEO stacks (always 24P or 48P)."""

    max_port = 0
    for ifname in interfaces:
        if isinstance(ifname, str):
            idx = _extract_if_index(ifname)
            if idx and idx > max_port:
                max_port = idx

    if max_port and max_port <= 24:
        return "C9300-24P"

    # Default to 48P if no port information is available or it exceeds 24.
    return "C9300-48P"

# ---------- Parsers de los logs ----------

EQUIPO_RE = re.compile(r"^\s*Equipo:\s*([A-Za-z0-9\-\._/]+)", re.IGNORECASE)

# Show interface status
INT_STATUS_HEADER_RE = re.compile(r"^\s*Port\s+Name\s+Status\s+Vlan\s+Duplex", re.IGNORECASE)
INT_STATUS_ROW_RE    = re.compile(
    r"^\s*(?P<port>(?:Fa|Gi|Te|Tw|Twe)\d+(?:/\d+){0,2})\s+(?P<name>.*?)\s+"
    r"(?P<status>connected|notconnect|disabled)\s+(?P<vlan>\S+)\s+",
    re.IGNORECASE
)

# Show mac address-table
MAC_HEADER_RE = re.compile(r"^\s*Vlan\s+Mac\s+Address\s+Type\s+Ports", re.IGNORECASE)
MAC_ROW_RE    = re.compile(
    r"^\s*(?P<vlan>\S+)\s+(?P<mac>[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4})\s+"
    r"(?P<type>STATIC|DYNAMIC)\s+(?P<port>.+?)\s*$", re.IGNORECASE
)

TIMESTAMP_PREFIX_RE = re.compile(r"^\s*\d{1,2}/\d{1,2}(?:/\d{2,4})?\[\d{2}:\d{2}:\d{2}\]")

LINE_PROTOCOL_RE = re.compile(
    r"^\s*(?P<ifname>\S+)\s+is\s+\S+,\s+line\s+protocol\s+is\s+\S+",
    re.IGNORECASE,
)
LAST_IO_RE = re.compile(
    r"^\s*Last\s+input\s+(?P<last>[^,]+),\s*output\s+(?P<output>[^,]+)",
    re.IGNORECASE,
)

# show running-config (interfaces)
IFACE_START_RE = re.compile(r"^\s*interface\s+(\S+)", re.IGNORECASE)
VOICE_VLAN_RE  = re.compile(r"^\s*switchport\s+voice\s+vlan\s+(\d+)", re.IGNORECASE)

SNMP_LOCATION_RE         = re.compile(r"^\s*snmp-server\s+location\s+(.+)$", re.IGNORECASE)
SNMP_CONTACT_RE          = re.compile(r"^\s*snmp-server\s+contact\s+(.+)$", re.IGNORECASE)
IP_DEFAULT_GATEWAY_RE    = re.compile(r"^\s*ip\s+default-gateway\s+(\S+)", re.IGNORECASE)
DEFAULT_GATEWAY_LINE_RE  = re.compile(r"^(\s*ip\s+default-gateway\s+)(\S+)(.*)$", re.IGNORECASE)
IP_PIM_REGISTER_RE       = re.compile(r"^\s*ip\s+pim\s+register-source\s+(\S+)", re.IGNORECASE)
IP_PIM_RP_RE             = re.compile(r"^\s*ip\s+pim\s+rp-address\s+.+$", re.IGNORECASE)
IP_TFTP_SOURCE_RE        = re.compile(r"^\s*ip\s+tftp\s+source-interface\s+(\S+)", re.IGNORECASE)
IP_HTTP_CLIENT_SOURCE_RE = re.compile(r"^\s*ip\s+http\s+client\s+source-interface\s+(\S+)", re.IGNORECASE)
SNMP_SOURCE_IF_RE        = re.compile(r"^\s*snmp-server\s+source-interface\s+traps\s+(\S+)", re.IGNORECASE)
NTP_SOURCE_RE            = re.compile(r"^\s*ntp\s+source\s+(\S+)", re.IGNORECASE)
IP_NAME_SERVER_RE        = re.compile(r"^\s*ip\s+name-server\s+(\S+)", re.IGNORECASE)
TRUNK_ALLOWED_RE = re.compile(
    r"^\s*switchport\s+trunk\s+allowed\s+vlan\s+(?:add\s+)?(.+)$",
    re.IGNORECASE,
)
ORIGIN_TAG_RE = re.compile(r"ORIGIN=([A-Z_]+)", re.IGNORECASE)

IP_INT_BRIEF_ROW_RE = re.compile(
    r"^\s*(?P<ifname>(?:Loopback\d+|(?:Fa|Gi|Te|Tw|Twe)\d+(?:/\d+){0,2}))\s+"
    r"(?P<ip>\S+)\s+YES\s+\S+\s+(?P<status>\S+)\s+(?P<protocol>\S+)\s*$",
    re.IGNORECASE,
)

# --- Filtros de seguridad / parsing ---
STICKY_LINE_RE        = re.compile(r"^\s*switchport\s+port-?security.*sticky\b", re.IGNORECASE)
BASE_HOSTNAME_RE      = re.compile(r"^\s*hostname\s+\S+", re.IGNORECASE)
HOSTNAME_CAPTURE_RE   = re.compile(r"^\s*hostname\s+(\S+)", re.IGNORECASE)
HOSTNAME_TOKEN_RE     = re.compile(
    r"(?P<center>\d{3})C\d{3,4}G-(?P<rack>\d{4})(?P<u>\d{2})?(?P<suffix>[A-Za-z0-9]{0,2})",
    re.IGNORECASE,
)
LOCATION_RACK_RE      = re.compile(r"R\s*(\d{2})(\d{2})", re.IGNORECASE)
LOCATION_U_RE         = re.compile(r"U\s*(\d{2})", re.IGNORECASE)

# ---------- Utilidades ----------

def to_short_ifname(ifname: str) -> str:
    s = ifname.strip()
    low = s.lower()
    if low.startswith("tengigabitethernet"):
        return "Te" + s[len("tengigabitethernet"):]
    if low.startswith("twogigabitethernet"):
        return "Tw" + s[len("twogigabitethernet"):]
    if low.startswith("twentyfivegige"):
        return "Twe" + s[len("twentyfivegige"):]
    if low.startswith("twentyfivegigabitethernet"):
        return "Twe" + s[len("twentyfivegigabitethernet"):]
    if low.startswith("gigabitethernet"):
        return "Gi" + s[len("gigabitethernet"):]
    if low.startswith("fastethernet"):
        return "Fa" + s[len("fastethernet"):]
    m = re.match(r"(?:Fa|Gi|Te)\d+(?:/\d+){0,2}", s, re.IGNORECASE)
    return m.group(0) if m else s

def normalize_port(p: str) -> str:
    p = p.strip()
    m = re.search(r"(Fa|Gi|Te|Tw|Twe)\d+(?:/\d+){0,2}", p, re.IGNORECASE)
    return m.group(0) if m else ""


def _strip_timestamp_prefix(line: str) -> str:
    """Elimina prefijos de marcas temporales tipo '29/10[15:35:26]' al inicio de la línea."""
    m = TIMESTAMP_PREFIX_RE.match(line)
    if not m:
        return line
    return line[m.end():]

# ---------- Helpers de nombres/interfaz ----------

def _format_numbered_name(base_name: str, ordinal: int) -> str:
    m = re.match(r"^(.*?)(\d+)$", base_name)
    if m:
        width = len(m.group(2))
        return f"{m.group(1)}{ordinal:0{width}d}"
    base = base_name.rstrip("-")
    return f"{base}-{ordinal:02d}" if base else f"{ordinal:02d}"


def _extract_hostname_tokens(value: str) -> Optional[Dict[str, Optional[str]]]:
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    cleaned = cleaned.strip("[]")
    match = HOSTNAME_TOKEN_RE.search(cleaned)
    if not match:
        return None
    rack = match.group("rack") or ""
    if len(rack) > 4:
        rack = rack[:4]
    tokens: Dict[str, Optional[str]] = {
        "center": match.group("center"),
        "rack": rack,
        "u": match.group("u"),
        "suffix": match.group("suffix"),
    }
    return tokens


def _extract_location_tokens(value: Optional[str]) -> Dict[str, Optional[str]]:
    if not value:
        return {}
    rack_match = LOCATION_RACK_RE.search(value)
    u_match = LOCATION_U_RE.search(value)
    tokens: Dict[str, Optional[str]] = {}
    if rack_match:
        tokens["rack"] = f"{rack_match.group(1)}{rack_match.group(2)}"
    if u_match:
        tokens["u"] = u_match.group(1)
    return tokens


def _choose_hostname_prefix(
    metadata: Dict[str, Dict[str, Any]],
    switch_number: Union[int, str],
) -> Tuple[str, str]:
    center_candidates: Counter = Counter()
    rack_candidates: Counter = Counter()

    for meta in metadata.values():
        host_value = meta.get("hostname") or meta.get("host")
        tokens = _extract_hostname_tokens(host_value) if host_value else None
        if tokens:
            if tokens.get("center"):
                center_candidates[tokens["center"]] += 1
            if tokens.get("rack"):
                rack_candidates[tokens["rack"]] += 1
        loc_tokens = _extract_location_tokens(meta.get("snmp_location"))
        if loc_tokens.get("rack"):
            rack_candidates[loc_tokens["rack"]] += 1

    def _resolve(counter: Counter, default: str) -> str:
        if not counter:
            return default
        most_common = counter.most_common(1)[0][0]
        return most_common

    switch_str = str(switch_number).strip()
    try:
        switch_int = int(switch_str)
    except ValueError:
        switch_int = None

    if switch_int is not None and switch_int < 1000:
        default_center = f"{switch_int:03d}"
    else:
        default_center = switch_str or "000"

    center = _resolve(center_candidates, default_center)

    if not rack_candidates:
        # Fallback: derive rack from the first hostname tokens if possible
        for meta in metadata.values():
            host_value = meta.get("hostname") or meta.get("host")
            tokens = _extract_hostname_tokens(host_value) if host_value else None
            if tokens and tokens.get("rack"):
                rack_candidates[tokens["rack"]] += 1
                break

    rack = _resolve(rack_candidates, "0000")

    center = re.sub(r"[^A-Za-z0-9]", "", center or "000")
    rack = re.sub(r"[^A-Za-z0-9]", "", rack or "0000")

    if center.isdigit() and len(center) < 3:
        center = center.zfill(3)
    if rack.isdigit() and len(rack) < 4:
        rack = rack.zfill(4)

    return center, rack


def build_hostname_plan(
    metadata: Dict[str, Dict[str, Any]],
    switch_number: Union[int, str],
    *,
    include_poe: bool,
    include_ambar_t: bool,
    include_uca: bool,
    include_video: bool,
) -> Dict[str, str]:
    center, rack = _choose_hostname_prefix(metadata, switch_number)
    base_prefix = f"{center}C9300G-{rack}"

    plan: Dict[str, str] = {}
    ambar_counter = 1

    if include_poe:
        plan["POE"] = f"{base_prefix}{ambar_counter:02d}A"
        ambar_counter += 1

    if include_ambar_t:
        plan["AMBAR_T"] = f"{base_prefix}{ambar_counter:02d}A"
        ambar_counter += 1

    if include_uca:
        plan["UCA"] = f"{base_prefix}02U"

    if include_video:
        plan["VIDEO"] = f"{base_prefix}03V"

    return plan

def _resolve_new_interface(builder: Union[Callable[[int], str], str], idx: int) -> str:
    if callable(builder):
        return builder(idx)
    return f"{builder}{idx}"

# ---------- Parseos de running-config ----------

def parse_voice_vlans_from_running_config(lines: List[str]) -> Dict[str, str]:
    voice_map: Dict[str, str] = {}
    current_if: Optional[str] = None
    for line in lines:
        m_if = IFACE_START_RE.search(line)
        if m_if:
            current_if = to_short_ifname(m_if.group(1))
            continue
        if current_if:
            m_v = VOICE_VLAN_RE.search(line)
            if m_v:
                voice_map[current_if] = m_v.group(1)
            if line.strip() == "!" or (line and not line[0].isspace()):
                current_if = None
    return voice_map

def parse_interface_configs_from_running_config(lines: List[str]) -> Dict[str, List[str]]:
    iface_cfg: Dict[str, List[str]] = {}
    current_if: Optional[str] = None
    buf: List[str] = []

    def flush():
        nonlocal current_if, buf
        if current_if is not None:
            iface_cfg[current_if] = buf[:]
        current_if, buf = None, []

    for raw in lines:
        m = IFACE_START_RE.search(raw)
        if m:
            flush()
            current_if = to_short_ifname(m.group(1))
            buf = []
            continue
        if current_if is not None:
            ln = raw.rstrip("\n")
            if ln.strip() == "!":
                flush()
            else:
                buf.append(ln)
    flush()
    return iface_cfg


def _block_forces_speed_10(block: Optional[List[str]]) -> bool:
    """Indica si la interfaz está configurada con velocidad fija a 10 Mbps."""
    if not block:
        return False
    for ln in block:
        if re.match(r"^\s*speed\s+10\s*$", ln, re.IGNORECASE):
            return True
    return False


def _description_from_block(block: Optional[List[str]]) -> Optional[str]:
    if not block:
        return None
    for ln in block:
        m = re.match(r"^\s*description\s+(.+)$", ln, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _expand_vlan_token(token: str) -> List[str]:
    token = token.strip()
    if not token or token.lower() in {"none", "all"}:
        return []
    if "-" in token:
        try:
            start, end = token.split("-", 1)
            start_i = int(start)
            end_i = int(end)
        except ValueError:
            return []
        if end_i < start_i:
            start_i, end_i = end_i, start_i
        return [str(v) for v in range(start_i, end_i + 1)]
    try:
        return [str(int(token))]
    except ValueError:
        return []


def _parse_allowed_vlans_from_block(block: Optional[List[str]]) -> List[str]:
    if not block:
        return []
    vlans: Set[str] = set()
    for line in block:
        m = TRUNK_ALLOWED_RE.search(line)
        if not m:
            continue
        raw_list = m.group(1)
        for part in raw_list.split(","):
            vlans.update(_expand_vlan_token(part))
    return sorted(vlans, key=int)


def _vlans_from_tag_string(tag_str: Optional[str]) -> Set[str]:
    if not tag_str or tag_str == "N/A":
        return set()
    vlans: Set[str] = set()
    for fragment in tag_str.split(";"):
        frag = fragment.strip()
        if not frag:
            continue
        if frag.upper().startswith("VLANS="):
            values = frag.split("=", 1)[1]
            for part in values.split(","):
                vlans.update(_expand_vlan_token(part))
    return vlans


def _origin_from_tag_string(tag_str: Optional[str]) -> Optional[str]:
    if not tag_str or tag_str == "N/A":
        return None
    match = ORIGIN_TAG_RE.search(tag_str)
    if match:
        return match.group(1).upper()
    return None


def parse_time_interval(value: str) -> Optional[int]:
    """Convierte cadenas de tiempo de IOS (e.g., '2w4d', '00:05:00') a segundos."""
    if value is None:
        return None
    val = value.strip().lower()
    if not val or val == "never":
        return None

    # Formato HH:MM:SS o MM:SS
    if ":" in val and all(part.isdigit() for part in val.split(":")):
        parts = [int(p) for p in val.split(":")]
        if len(parts) == 3:
            h, m, s = parts
        elif len(parts) == 2:
            h, m, s = 0, parts[0], parts[1]
        else:
            h, m, s = 0, 0, parts[0]
        return h * 3600 + m * 60 + s

    total = 0
    matched = False
    for number, suffix in re.findall(r"(\d+)\s*([a-z]+)", val):
        matched = True
        qty = int(number)
        if suffix.startswith("y"):
            total += qty * 365 * 24 * 3600
        elif suffix.startswith("w"):
            total += qty * 7 * 24 * 3600
        elif suffix.startswith("d"):
            total += qty * 24 * 3600
        elif suffix.startswith("h"):
            total += qty * 3600
        elif suffix.startswith("m"):
            total += qty * 60
        elif suffix.startswith("s"):
            total += qty
    if matched:
        return total

    if val.isdigit():
        return int(val)
    return None


def parse_last_io_information(lines: List[str]) -> Dict[str, Tuple[str, str, Optional[int]]]:
    """Obtiene {if_short: (last_input_raw, output_raw, last_input_seconds)}."""
    last_map: Dict[str, Tuple[str, str, Optional[int]]] = {}
    current_if: Optional[str] = None

    for raw in lines:
        m_proto = LINE_PROTOCOL_RE.search(raw)
        if m_proto:
            current_if = to_short_ifname(m_proto.group("ifname"))
            continue
        if current_if:
            m_last = LAST_IO_RE.search(raw)
            if m_last:
                last_raw = m_last.group("last").strip()
                output_raw = m_last.group("output").strip()
                seconds = parse_time_interval(last_raw)
                last_map[current_if] = (last_raw, output_raw, seconds)
                current_if = None
                continue
        if not raw.strip():
            current_if = None
            continue
        # Cualquier línea que no aporte datos reinicia el estado
        if LINE_PROTOCOL_RE.search(raw) is None and raw.strip().startswith("!"):
            current_if = None
    return last_map


def parse_global_metadata(lines: List[str], iface_cfg_map: Dict[str, List[str]]) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "snmp_location": None,
        "snmp_contact": None,
        "ip_default_gateway": None,
        "ip_pim_register_source": None,
        "ip_pim_rp_lines": [],
        "ip_tftp_source_interface": None,
        "ip_http_client_source_interface": None,
        "snmp_source_interface": None,
        "ntp_source": None,
        "ip_name_server_lines": [],
        "router_blocks": [],
        "vlan_blocks": [],
    }

    in_interface = False
    router_buffer: Optional[List[str]] = None
    current_vlan_block: Optional[List[str]] = None

    def flush_router_buffer() -> None:
        nonlocal router_buffer
        if router_buffer is not None:
            metadata["router_blocks"].append(router_buffer)
            router_buffer = None

    def flush_vlan_block() -> None:
        nonlocal current_vlan_block
        if current_vlan_block is not None:
            metadata["vlan_blocks"].append(current_vlan_block)
            current_vlan_block = None

    for raw in lines:
        stripped = raw.rstrip("\n")
        line = stripped.strip()
        lower = line.lower()

        if current_vlan_block is not None:
            if line == "!":
                current_vlan_block.append("!")
                flush_vlan_block()
                continue
            if stripped.startswith(" "):
                current_vlan_block.append(stripped)
                continue
            flush_vlan_block()

        if not line:
            continue

        if lower.startswith("interface "):
            in_interface = True
            flush_router_buffer()
            continue

        if line == "!":
            if in_interface:
                in_interface = False
            flush_router_buffer()
            continue

        if in_interface:
            continue

        if re.match(r"^vlan\s+[\d,\-]+", lower):
            flush_router_buffer()
            flush_vlan_block()
            current_vlan_block = [stripped]
            continue

        if lower.startswith("router "):
            flush_router_buffer()
            router_buffer = [stripped]
            continue

        if router_buffer is not None:
            router_buffer.append(stripped)
            continue

        m_loc = SNMP_LOCATION_RE.match(line)
        if m_loc:
            metadata["snmp_location"] = m_loc.group(1).strip()
            continue

        m_contact = SNMP_CONTACT_RE.match(line)
        if m_contact:
            metadata["snmp_contact"] = m_contact.group(1).strip()
            continue

        m_gw = IP_DEFAULT_GATEWAY_RE.match(line)
        if m_gw:
            metadata["ip_default_gateway"] = m_gw.group(1).strip()
            continue

        m_register = IP_PIM_REGISTER_RE.match(line)
        if m_register:
            metadata["ip_pim_register_source"] = to_short_ifname(m_register.group(1).strip())
            continue

        if IP_PIM_RP_RE.match(line):
            metadata["ip_pim_rp_lines"].append(stripped)
            continue

        m_tftp = IP_TFTP_SOURCE_RE.match(line)
        if m_tftp:
            metadata["ip_tftp_source_interface"] = to_short_ifname(m_tftp.group(1).strip())
            continue

        m_http = IP_HTTP_CLIENT_SOURCE_RE.match(line)
        if m_http:
            metadata["ip_http_client_source_interface"] = to_short_ifname(m_http.group(1).strip())
            continue

        m_snmp_src = SNMP_SOURCE_IF_RE.match(line)
        if m_snmp_src:
            metadata["snmp_source_interface"] = to_short_ifname(m_snmp_src.group(1).strip())
            continue

        m_ntp = NTP_SOURCE_RE.match(line)
        if m_ntp:
            metadata["ntp_source"] = to_short_ifname(m_ntp.group(1).strip())
            continue

        if IP_NAME_SERVER_RE.match(line):
            metadata["ip_name_server_lines"].append(stripped)
            continue

    flush_router_buffer()
    flush_vlan_block()

    management_candidate = (
        metadata.get("ip_tftp_source_interface")
        or metadata.get("ip_http_client_source_interface")
        or metadata.get("snmp_source_interface")
        or metadata.get("ntp_source")
    )
    metadata["management_interface"] = _guess_management_interface(iface_cfg_map, management_candidate)
    metadata["video_management_interface"] = _guess_video_management_vlan(
        iface_cfg_map, metadata["management_interface"]
    )

    return metadata


def _guess_management_interface(
    iface_cfg_map: Dict[str, List[str]],
    preferred: Optional[str],
) -> Optional[str]:
    if preferred:
        return to_short_ifname(preferred)

    vlan_candidates: List[str] = []
    for ifname, block in iface_cfg_map.items():
        short = to_short_ifname(ifname)
        if short.lower().startswith("vlan"):
            if any(re.search(r"\bip address\b", ln, re.IGNORECASE) for ln in block):
                vlan_candidates.append(short)
    if vlan_candidates:
        return vlan_candidates[0]

    if "Loopback0" in iface_cfg_map:
        return "Loopback0"

    return preferred


def _guess_video_management_vlan(
    iface_cfg_map: Dict[str, List[str]],
    preferred: Optional[str],
) -> Optional[str]:
    if preferred:
        short_pref = to_short_ifname(preferred)
        if short_pref.lower().startswith("vlan9"):
            return short_pref

    candidates: List[str] = []
    for ifname, block in iface_cfg_map.items():
        short = to_short_ifname(ifname)
        if not short.lower().startswith("vlan9"):
            continue
        if any(re.search(r"\bip address\b", ln, re.IGNORECASE) for ln in block):
            candidates.append(short)

    if candidates:
        def _vlan_sort_key(name: str) -> Tuple[int, str]:
            m = re.search(r"(\d+)", name)
            return (int(m.group(1)) if m else 0, name)

        return min(candidates, key=_vlan_sort_key)

    return None


def parse_video_interface_order(lines: List[str]) -> List[str]:
    """Devuelve el orden natural de interfaces para equipos de vídeo."""
    order: List[str] = []
    normalized = [_strip_timestamp_prefix(ln.rstrip("\n")) for ln in lines]
    in_status = False
    saw_status = False

    for ln in normalized:
        if INT_STATUS_HEADER_RE.search(ln):
            in_status = True
            saw_status = True
            continue
        if in_status:
            if not ln.strip():
                in_status = False
                continue
            m = INT_STATUS_ROW_RE.search(ln)
            if m:
                order.append(to_short_ifname(m.group("port").strip()))

    if saw_status and order:
        return order

    for ln in normalized:
        m = IP_INT_BRIEF_ROW_RE.search(ln)
        if not m:
            continue
        ifname = to_short_ifname(m.group("ifname"))
        if ifname.lower().startswith(("gi", "fa", "te", "tw", "twe", "lo")):
            order.append(ifname)
    return order

# ---------- Parser de logs ----------

def parse_log(filepath: str):
    hostname = None
    int_rows = []
    mac_static: Dict[str, List[str]] = {}
    mac_dynamic: Dict[str, List[str]] = {}

    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        raw_lines = f.readlines()

    lines = [_strip_timestamp_prefix(line.rstrip("\n")) for line in raw_lines]

    # Equipo:
    for line in lines:
        m = EQUIPO_RE.search(line)
        if m:
            hostname = m.group(1).strip()
            break
    if not hostname:
        for line in lines:
            m = HOSTNAME_CAPTURE_RE.search(line)
            if m:
                hostname = m.group(1).strip()
                break
    if not hostname:
        hostname = os.path.basename(filepath)

    # show int status
    in_int_status = False
    for line in lines:
        if INT_STATUS_HEADER_RE.search(line):
            in_int_status = True
            continue
        if in_int_status:
            if not line.strip():
                in_int_status = False
                continue
            m = INT_STATUS_ROW_RE.search(line)
            if m:
                int_rows.append({
                    "port":   m.group("port").strip(),
                    "name":   m.group("name").strip(),
                    "status": m.group("status").lower(),
                    "vlan":   m.group("vlan").strip()
                })

    # mac address-table
    in_mac = False
    for line in lines:
        if MAC_HEADER_RE.search(line):
            in_mac = True
            continue
        if in_mac:
            m = MAC_ROW_RE.search(line)
            if not m:
                continue
            port = normalize_port(m.group("port"))
            if not port:
                continue
            mac = m.group("mac").lower()
            mtype = m.group("type").upper()
            (mac_static if mtype == "STATIC" else mac_dynamic).setdefault(port, []).append(mac)

    mac_map = {p: mac_static.get(p) or mac_dynamic.get(p) or [] for p in set(mac_static) | set(mac_dynamic)}

    # running-config: voice por interfaz + bloques por interfaz
    voice_map     = parse_voice_vlans_from_running_config(lines)
    iface_cfg_map = parse_interface_configs_from_running_config(lines)
    last_io_map   = parse_last_io_information(lines)
    metadata      = parse_global_metadata(lines, iface_cfg_map)

    metadata["hostname"] = metadata.get("hostname") or hostname

    return hostname, int_rows, mac_map, voice_map, iface_cfg_map, last_io_map, metadata

# ---------- Inventarios ----------

def build_inventory_from_logs(filepaths: List[str]):
    """
    Devuelve:
      - wifi_items, voip_items, uca_items, ambar_other_items, trunk_items (listas de dict)
      - all_iface_cfgs: {(host, if_short): [líneas running sin 'interface' ni '!']}
    """
    wifi_items, voip_items, uca_items, ambar_other_items, trunk_items = [], [], [], [], []
    all_iface_cfgs: Dict[Tuple[str, str], List[str]] = {}
    host_metadata: Dict[str, Dict[str, Any]] = {}
    skipped_ports: List[Dict[str, Any]] = []

    for fp in filepaths:
        host, int_rows, mac_map, voice_map, iface_cfg_map, last_io_map, metadata = parse_log(fp)

        host_metadata[host] = metadata

        # Guarda bloques running por interfaz
        for if_short, block in iface_cfg_map.items():
            all_iface_cfgs[(host, if_short)] = block

        description_map = {
            if_short: _description_from_block(block)
            for if_short, block in iface_cfg_map.items()
        }

        for r in int_rows:
            status_display = r["status"]
            status = status_display.lower()
            port_short = r["port"]
            last_info = last_io_map.get(port_short)
            block = iface_cfg_map.get(port_short)

            def _has_recent_activity(info: Optional[Tuple[str, str, Optional[int]]]) -> bool:
                if not info:
                    return False
                last_raw, output_raw, last_seconds = info
                if last_raw and output_raw and last_raw.strip().lower() == "never" and output_raw.strip().lower() == "never":
                    return False
                return last_seconds is not None and last_seconds < INACTIVITY_THRESHOLD_SECONDS

            def _register_skip(reason_code: str) -> None:
                last_raw = last_info[0] if last_info else None
                output_raw = last_info[1] if last_info else None
                last_seconds = last_info[2] if last_info else None
                skipped_ports.append(
                    {
                        "host": host,
                        "hostname": metadata.get("hostname") or host,
                        "port": port_short,
                        "status": status_display,
                        "reason": reason_code,
                        "last_raw": last_raw,
                        "output_raw": output_raw,
                        "last_seconds": last_seconds,
                        "name": r.get("name"),
                    }
                )

            if status != "connected":
                if not (status == "notconnect" and _has_recent_activity(last_info)):
                    _register_skip("status_not_connected")
                    continue

            vlan = r["vlan"].strip()
            if vlan in IGNORED_VLANS:
                continue
            voice_vlan = voice_map.get(port_short)
            voice_tag = voice_vlan or (vlan if vlan in VOIP_VLANS else None)
            forces_speed_10 = _block_forces_speed_10(block)

            vlan_lc = vlan.lower()
            if last_info:
                last_raw, output_raw, last_seconds = last_info
                if (
                    last_raw and output_raw
                    and last_raw.strip().lower() == "never"
                    and output_raw.strip().lower() == "never"
                ):
                    _register_skip("never")
                    continue
                if (
                    last_seconds is not None
                    and last_seconds >= INACTIVITY_THRESHOLD_SECONDS
                    and status != "connected"
                ):
                    _register_skip("inactive_threshold")
                    continue

            description = description_map.get(port_short)
            if vlan_lc == "trunk":
                idx_value = _extract_if_index(port_short)
                if idx_value is None or idx_value > 48:
                    continue
                block = iface_cfg_map.get(port_short)
                name_field = description or (r["name"] if r["name"] else "")
                name_lower = name_field.lower()
                if "uplink" in name_lower or "core" in name_lower:
                    continue
                if block and any(
                    re.search(r"\bchannel-group\b", ln, re.IGNORECASE) for ln in block
                ):
                    continue
                allowed_vlans = _parse_allowed_vlans_from_block(block)
                trunk_items.append({
                    "src_host": host,
                    "src_port": port_short,
                    "name": name_field if name_field else "N/A",
                    "vlan": "trunk",
                    "macs": mac_map.get(port_short, []),
                    "voice_vlan": None,
                    "mode": "trunk",
                    "allowed_vlans": allowed_vlans,
                    "origin": "TRUNK",
                })
                continue

            if not vlan.isdigit():
                # Formatos raros no numéricos -> descartar
                continue

            name_field = description or (r["name"] if r["name"] else "N/A")
            item = {
                "src_host": host,
                "src_port": port_short,
                "name": name_field,
                "vlan": vlan,
                "macs": mac_map.get(port_short, []),
                "voice_vlan": voice_tag,
                "mode": "access",
            }
            if forces_speed_10:
                item["avoid_uxm"] = True
                item["speed_10"] = True
                item["origin"] = "AMBAR"
                ambar_other_items.append(item)
                continue

            if vlan in WIFI_VLANS:
                item["origin"] = "WIFI"
                wifi_items.append(item)
            elif voice_vlan or vlan in VOIP_VLANS:
                item["origin"] = "VOIP"
                voip_items.append(item)
            elif vlan in UCA_VLANS:
                item["origin"] = "UCA"
                uca_items.append(item)
            else:
                item["origin"] = "AMBAR"
                ambar_other_items.append(item)

    def port_key(p):
        nums = re.findall(r"\d+", p)
        return tuple(int(x) for x in nums) if nums else (9999,)

    wifi_items.sort(key=lambda x: (x["src_host"], port_key(x["src_port"])))
    voip_items.sort(key=lambda x: (x["src_host"], port_key(x["src_port"])))
    uca_items.sort(key=lambda x: (x["src_host"], port_key(x["src_port"])))
    ambar_other_items.sort(key=lambda x: (x["src_host"], port_key(x["src_port"])))
    trunk_items.sort(key=lambda x: (x["src_host"], port_key(x["src_port"])))
    skipped_ports.sort(
        key=lambda x: (
            (x.get("hostname") or x.get("host") or ""),
            port_key(x.get("port") or ""),
        )
    )
    return (
        wifi_items,
        voip_items,
        uca_items,
        ambar_other_items,
        trunk_items,
        all_iface_cfgs,
        host_metadata,
        skipped_ports,
    )


# ---------- Mapeos ----------

def _mk_row(new_if_builder, idx_new, item, sw_name, group_tag):
    new_if = _resolve_new_interface(new_if_builder, idx_new)
    mac_str = ", ".join(item["macs"]) if item["macs"] else "N/A"
    tags: List[str] = []
    voice_vlan = item.get("voice_vlan")
    if voice_vlan:
        tags.append(f"VOICE={voice_vlan}")
    allowed_vlans = item.get("allowed_vlans")
    if allowed_vlans:
        try:
            ordered = sorted(allowed_vlans, key=int)
        except ValueError:
            ordered = sorted(allowed_vlans)
        tags.append(f"VLANS={','.join(ordered)}")
    if item.get("speed_10"):
        tags.append("SPEED=10")
    origin = item.get("origin")
    if origin:
        tags.append(f"ORIGIN={origin}")
    tag_str = ";".join(tags) if tags else "N/A"
    return [
        item["src_host"],           # SW Actual
        item["src_port"],           # Interface actual
        item["name"],               # Description actual
        new_if,                      # Interface nuevo
        item["name"],               # Description nueva
        sw_name,                     # SW Nuevo (interno / etiqueta)
        item.get("vlan", "LIBRE"),  # VLAN (informativo)
        item.get("mode", "access"),# Mode
        tag_str,                     # Tags (p.ej., VOICE=1229)
        mac_str,                     # MAC Actual
        group_tag                    # _Grupo
    ]

def _poe_switch_name_for_member(member_index: int) -> str:
    meta = POE_MEMBER_METADATA.get(member_index)
    if meta:
        return str(meta.get("sw_name"))
    if member_index == 1:
        return NEW_SWITCH_POE_NAME
    return _format_numbered_name(NEW_SWITCH_POE_NAME, member_index)

def _format_poe_interface(member_index: int, port_idx: int) -> str:
    meta = POE_MEMBER_METADATA.get(member_index, {})
    member_type = meta.get("type")
    if member_type == "wifi":
        if port_idx <= WIFI_TWOGIG_LIMIT:
            prefix = f"TwoGigabitEthernet{member_index}/0/"
        else:
            prefix = f"TenGigabitEthernet{member_index}/0/"
    else:
        prefix = f"GigabitEthernet{member_index}/0/"
    return f"{prefix}{port_idx}"

def _poe_interface_formatter(member_index: int) -> Callable[[int], str]:
    return lambda port_idx, member=member_index: _format_poe_interface(member, port_idx)

def _libre_row(builder, idx: int, sw_name: str, group_tag: str):
    new_if = _resolve_new_interface(builder, idx)
    return ["LIBRE","LIBRE","LIBRE",new_if,"LIBRE",sw_name,"LIBRE","access","LIBRE","N/A",group_tag]

def _pad_member_with_libres(rows, builder, sw_name, start_idx, capacity, group_tag):
    idx = start_idx
    while idx <= capacity:
        rows.append(_libre_row(builder, idx, sw_name, group_tag))
        idx += 1

def _poe_member_capacities(required_slots: int):
    capacities: List[int] = []
    remaining = max(0, required_slots)
    member_index = 0
    while remaining > 0 and member_index < POE_MAX_STACK_MEMBERS:
        member_index += 1
        if member_index < POE_MAX_STACK_MEMBERS:
            cap = POE_MAX_PORTS
        else:
            # Último miembro: usa 24 puertos (22 efectivos) si lo restante cabe, si no 48.
            usable_in_aux = max(POE_AUX_FINAL_PORTS - 2, 0)
            cap = POE_MAX_PORTS if remaining > usable_in_aux else POE_AUX_FINAL_PORTS
        capacities.append(cap)
        remaining = max(0, remaining - max(cap - 2, 0))
    if not capacities and required_slots > 0:
        capacities.append(POE_MAX_PORTS)
    return capacities

def make_mapping_poe(wifi_items, voip_items, ambar_others, trunk_items):
    """
    Cambios:
      - Los AMBAR con speed 10 (avoid_uxm=True) se colocan justo después del bloque AMBAR normal,
        antes de los TRUNK, y nunca en el miembro UXM (wifi).
    """
    global POE_MEMBER_METADATA
    has_wifi = bool(wifi_items)
    has_voip = bool(voip_items)
    reserve_after_wifi = RESERVED_AFTER_WIFI if has_wifi else 0
    reserve_after_voip = RESERVED_AFTER_VOIP if has_voip else 0
    tail_free = RESERVED_TAIL_FREE if (has_wifi or has_voip) else 0

    core_slots = (
        len(wifi_items)
        + reserve_after_wifi
        + len(voip_items)
        + reserve_after_voip
        + len(trunk_items)
        + tail_free
    )
    required_slots = core_slots
    if required_slots == 0 and ambar_others:
        max_stack_capacity = POE_MAX_PORTS * POE_MAX_STACK_MEMBERS
        required_slots = min(len(ambar_others), max_stack_capacity)

    capacities = _poe_member_capacities(required_slots)
    if not capacities:
        POE_MEMBER_METADATA.clear()
        return [], ambar_others

    avoid_requires_poe = False
    if has_wifi:
        for collection in (wifi_items, voip_items, ambar_others, trunk_items):
            if any(item.get("avoid_uxm") for item in collection):
                avoid_requires_poe = True
                break

    if avoid_requires_poe and len(capacities) == 1:
        usable_aux = max(POE_AUX_FINAL_PORTS - 2, 0)
        avoid_count = sum(1 for coll in (wifi_items, voip_items, ambar_others, trunk_items) for item in coll if item.get("avoid_uxm"))
        extra_capacity = POE_AUX_FINAL_PORTS if avoid_count <= usable_aux else POE_MAX_PORTS
        capacities.append(extra_capacity)

    def _build_meta(caps: List[int]) -> Tuple[List[Dict[str, Any]], int]:
        meta: List[Dict[str, Any]] = []
        total = 0
        for member_index, capacity in enumerate(caps, start=1):
            member_type = "wifi" if has_wifi and member_index == 1 else "poe"
            usable = max(capacity - 2, 0)
            meta.append({"index": member_index, "capacity": capacity, "type": member_type, "usable": usable})
            total += usable
        return meta, total

    meta_info, total_usable = _build_meta(capacities)

    while len(capacities) < POE_MAX_STACK_MEMBERS:
        usable_for_ambar = max(total_usable - core_slots, 0)
        if usable_for_ambar >= len(ambar_others):
            break
        remaining_needed = len(ambar_others) - usable_for_ambar
        if remaining_needed <= 0:
            break
        next_capacity = (
            POE_MAX_PORTS
            if (len(capacities) + 1) < POE_MAX_STACK_MEMBERS or remaining_needed > POE_AUX_FINAL_PORTS
            else POE_AUX_FINAL_PORTS
        )
        capacities.append(next_capacity)
        meta_info, total_usable = _build_meta(capacities)

    # Construye metadata y nombres de miembros
    POE_MEMBER_METADATA.clear()
    wifi_counter = 0
    poe_counter = 0
    for info in meta_info:
        member_index = info["index"]
        capacity = info["capacity"]
        if info["type"] == "wifi":
            wifi_counter += 1
            sw_name = _format_numbered_name(NEW_SWITCH_WIFI_NAME, wifi_counter)
        else:
            poe_counter += 1
            sw_name = _format_numbered_name(NEW_SWITCH_POE_NAME, poe_counter)
        POE_MEMBER_METADATA[member_index] = {
            "type": info["type"],
            "sw_name": sw_name,
            "capacity": capacity,
            "usable_capacity": max(capacity - 2, 0),
        }

    total_usable = sum(info["usable_capacity"] for info in POE_MEMBER_METADATA.values())

    if core_slots > total_usable:
        deficit = core_slots - total_usable
        if tail_free > 0:
            reclaim = min(deficit, tail_free)
            tail_free -= reclaim
            deficit -= reclaim
        if deficit > 0 and reserve_after_voip > 0:
            reclaim = min(deficit, reserve_after_voip)
            reserve_after_voip -= reclaim
            deficit -= reclaim
        if deficit > 0 and reserve_after_wifi > 0:
            reclaim = min(deficit, reserve_after_wifi)
            reserve_after_wifi -= reclaim
            deficit -= reclaim
        core_slots = (
            len(wifi_items)
            + reserve_after_wifi
            + len(voip_items)
            + reserve_after_voip
            + len(trunk_items)
            + tail_free
        )
        if deficit > 0 or core_slots > total_usable:
            raise RuntimeError("Capacidad POE insuficiente para WIFI/VoIP/AMBAR/TRUNK configurados")

    # Particiones y orden
    wifi_primary = [it for it in wifi_items if not it.get("avoid_uxm")]
    wifi_avoid   = [it for it in wifi_items if it.get("avoid_uxm")]
    voip_primary = [it for it in voip_items if not it.get("avoid_uxm")]
    voip_avoid   = [it for it in voip_items if it.get("avoid_uxm")]

    # AMBAR que caben en POE
    ambar_capacity = max(total_usable - core_slots, 0)
    ambar_for_poe = ambar_others[:ambar_capacity]
    ambar_overflow = ambar_others[ambar_capacity:]

    ambar_primary: List[Dict[str, Any]] = []
    ambar_slow:    List[Dict[str, Any]] = []  # <-- speed 10
    for it in ambar_for_poe:
        (ambar_slow if it.get("avoid_uxm") else ambar_primary).append(it)

    def _split_by_poe_requirement(items: List[Dict[str, Any]]):
        poe_required: List[Dict[str, Any]] = []
        optional: List[Dict[str, Any]] = []
        for entry in items:
            (poe_required if _item_requires_poe_support(entry) else optional).append(entry)
        return poe_required, optional

    ambar_primary_poe, ambar_primary_optional = _split_by_poe_requirement(ambar_primary)
    ambar_slow_poe, ambar_slow_optional = _split_by_poe_requirement(ambar_slow)

    trunk_primary = [it for it in trunk_items if not it.get("avoid_uxm")]
    trunk_avoid   = [it for it in trunk_items if it.get("avoid_uxm")]

    # Los avoid restantes (NO speed10 de AMBAR, porque ya van como AMBAR_SLOW antes de trunks)
    avoid_sequence: List[Dict[str, Any]] = []
    avoid_sequence.extend(wifi_avoid)
    avoid_sequence.extend(voip_avoid)
    avoid_sequence.extend(trunk_avoid)

    # Secuencia final
    sequence: List[Tuple[str, Optional[Dict[str, Any]]]] = []
    sequence.extend(("ITEM", it) for it in wifi_primary)
    sequence.extend(("LIBRE", None) for _ in range(reserve_after_wifi))
    sequence.extend(("ITEM", it) for it in voip_primary)
    sequence.extend(("LIBRE", None) for _ in range(reserve_after_voip))
    sequence.extend(("ITEM", it) for it in ambar_primary_poe)
    sequence.extend(("ITEM", it) for it in ambar_primary_optional)
    sequence.extend(("AMBAR_SLOW", it) for it in ambar_slow_poe)   # <-- aquí: antes de TRUNK
    sequence.extend(("AMBAR_SLOW", it) for it in ambar_slow_optional)
    sequence.extend(("ITEM", it) for it in trunk_primary)
    if avoid_sequence:
        sequence.append(("FORCE_NEXT", None))
        sequence.extend(("ITEM", it) for it in avoid_sequence)
    sequence.extend(("LIBRE", None) for _ in range(tail_free))

    # Emisión de filas a miembros/puertos
    rows: List[List[str]] = []
    member_index = 1
    member_info = POE_MEMBER_METADATA[member_index]
    usable_capacity = int(member_info["usable_capacity"])
    total_ports = int(member_info["capacity"])
    formatter = _poe_interface_formatter(member_index)
    sw_name = str(member_info["sw_name"])
    idx = 1

    def flush_current(start_idx: int) -> None:
        if usable_capacity > 0 and start_idx <= usable_capacity:
            _pad_member_with_libres(rows, formatter, sw_name, start_idx, usable_capacity, "POE")
        _pad_member_with_libres(rows, formatter, sw_name, usable_capacity + 1, total_ports, "POE")

    def goto_next_member():
        nonlocal member_index, member_info, usable_capacity, total_ports, formatter, sw_name, idx
        flush_current(idx)
        member_index += 1
        if member_index > len(capacities):
            raise RuntimeError("Capacidad POE insuficiente para la secuencia generada")
        member_info = POE_MEMBER_METADATA[member_index]
        usable_capacity = int(member_info["usable_capacity"])
        total_ports = int(member_info["capacity"])
        formatter = _poe_interface_formatter(member_index)
        sw_name = str(member_info["sw_name"])
        idx = 1

    for kind, payload in sequence:
        if kind == "FORCE_NEXT":
            goto_next_member()
            continue

        # Si estamos fuera de capacidad útil, saltamos de miembro
        while usable_capacity <= 0 or idx > usable_capacity:
            goto_next_member()

        if kind == "AMBAR_SLOW":
            # Política: NUNCA en UXM (miembro wifi)
            if member_info.get("type") == "wifi":
                goto_next_member()
                # puede haber más de un miembro wifi? aquí solo el 1º; rechecamos por si acaso
                if member_info.get("type") == "wifi":
                    goto_next_member()
            rows.append(_mk_row(formatter, idx, payload, sw_name, "POE"))
            idx += 1
            continue

        if kind == "ITEM":
            rows.append(_mk_row(formatter, idx, payload, sw_name, "POE"))
        else:  # LIBRE
            rows.append(_libre_row(formatter, idx, sw_name, "POE"))
        idx += 1

    flush_current(idx)
    while member_index < len(capacities):
        goto_next_member()
        flush_current(1)

    # Actualiza el tipo de cada miembro en base a los orígenes asignados.
    member_roles: Dict[int, str] = {}
    for idx_meta, meta in POE_MEMBER_METADATA.items():
        if idx_meta <= 1:
            continue
        if not isinstance(meta, dict):
            continue
        member_roles[idx_meta] = "data"

    for row in rows:
        if not row or row[-1] != "POE":
            continue
        if str(row[0]).upper() == "LIBRE":
            continue
        member_idx = _extract_member_index(row[3])
        if member_idx is None or member_idx <= 1:
            continue
        if member_idx not in member_roles:
            continue
        tags_field = row[8]
        origin = None
        if isinstance(tags_field, str) and tags_field and tags_field.upper() != "N/A":
            for part in tags_field.split(";"):
                if not part:
                    continue
                key_val = part.split("=", 1)
                if len(key_val) != 2:
                    continue
                key = key_val[0].strip().upper()
                if key == "ORIGIN":
                    origin = key_val[1].strip().upper()
                    break
        if isinstance(row[6], str):
            vlan_value = row[6].strip()
            if vlan_value in MANDATORY_POE_VLANS:
                member_roles[member_idx] = "poe"
                continue

        if origin in {"WIFI", "VOIP"}:
            member_roles[member_idx] = "poe"

    for idx_meta, role in member_roles.items():
        meta = POE_MEMBER_METADATA.get(idx_meta)
        if not isinstance(meta, dict):
            continue
        meta["type"] = role

    _relocate_poe_trunks(rows)

    return rows, ambar_overflow

def _ambar_t_switch_capacities(required_ports: int) -> List[int]:
    capacities: List[int] = []
    remaining = required_ports
    usable_default = max(AMBAR_T_DEFAULT_PORTS - AMBAR_T_RESERVED_TAIL, 0)
    usable_large = max(AMBAR_T_LARGE_PORTS - AMBAR_T_RESERVED_TAIL, 0)

    if remaining <= 0:
        return [AMBAR_T_DEFAULT_PORTS]

    while remaining > 0:
        if usable_large and remaining > usable_default:
            cap = AMBAR_T_LARGE_PORTS
            usable = usable_large
        else:
            cap = AMBAR_T_DEFAULT_PORTS
            usable = usable_default

        if usable <= 0:
            cap = AMBAR_T_LARGE_PORTS
            usable = max(cap - AMBAR_T_RESERVED_TAIL, 0)

        capacities.append(cap)
        remaining -= usable if usable > 0 else cap

    return capacities if capacities else [AMBAR_T_DEFAULT_PORTS]


def _ambar_t_interface_formatter(member_index: int) -> Callable[[int], str]:
    return lambda port_idx, member=member_index: f"GigabitEthernet{member}/0/{port_idx}"


def make_mapping_ambar_t(ambar_items):
    if not ambar_items:
        return []

    capacities = _ambar_t_switch_capacities(len(ambar_items))
    rows: List[List[str]] = []
    idx_item = 0
    switch_ordinal = 1

    for capacity in capacities:
        sw_name = _format_numbered_name(NEW_SWITCH_AMBAR_T_NAME, switch_ordinal)
        formatter = _ambar_t_interface_formatter(switch_ordinal)
        usable = max(capacity - AMBAR_T_RESERVED_TAIL, 0)
        for port_idx in range(1, capacity + 1):
            if port_idx <= usable and idx_item < len(ambar_items):
                rows.append(_mk_row(formatter, port_idx, ambar_items[idx_item], sw_name, "AMBAR_T"))
                idx_item += 1
            else:
                rows.append(_libre_row(formatter, port_idx, sw_name, "AMBAR_T"))
        switch_ordinal += 1

    return rows

def _uca_interface_formatter(member_index: int) -> Callable[[int], str]:
    return lambda port_idx, member=member_index: f"GigabitEthernet{member}/0/{port_idx}"

def _uca_switch_capacities(required_ports: int, reserved_tail: int) -> List[int]:
    capacities: List[int] = []
    remaining = required_ports
    usable_default = max(UCA_DEFAULT_PORTS - reserved_tail, 0)
    usable_large = max(UCA_LARGE_PORTS - reserved_tail, 0)

    if remaining <= 0:
        return [UCA_DEFAULT_PORTS]

    while remaining > 0:
        if usable_large and remaining > usable_default:
            cap = UCA_LARGE_PORTS
            usable = usable_large
        else:
            cap = UCA_DEFAULT_PORTS
            usable = usable_default

        if usable <= 0:
            cap = UCA_LARGE_PORTS
            usable = max(cap - reserved_tail, 0)

        capacities.append(cap)
        remaining -= usable if usable > 0 else cap

    return capacities if capacities else [UCA_DEFAULT_PORTS]

def make_mapping_uca(uca_items, reserved_tail: int):
    total_items = len(uca_items)
    capacities = _uca_switch_capacities(total_items, reserved_tail)
    rows: List[List[str]] = []
    idx_item = 0
    switch_ordinal = 1
    for capacity in capacities:
        sw_name = _format_numbered_name(NEW_SWITCH_UCA_T_NAME, switch_ordinal)
        usable = max(capacity - reserved_tail, 0)
        formatter = _uca_interface_formatter(switch_ordinal)
        for port_idx in range(1, capacity + 1):
            if port_idx <= usable and idx_item < total_items:
                rows.append(_mk_row(formatter, port_idx, uca_items[idx_item], sw_name, "UCA"))
                idx_item += 1
            else:
                rows.append(_libre_row(formatter, port_idx, sw_name, "UCA"))
        switch_ordinal += 1

    return rows


def _collect_trunk_allowed_from_rows(rows: List[List[str]], group: str) -> Tuple[List[Tuple[int, List[str]]], List[str]]:
    entries: List[Tuple[int, List[str]]] = []
    allowed: Set[str] = set()

    for idx, row in enumerate(rows):
        if row[-1] != group:
            continue
        entries.append((idx, row))
        if str(row[0]).upper() == "LIBRE":
            continue
        vlan = row[6]
        if vlan and str(vlan).isdigit():
            allowed.add(str(vlan))
        allowed.update(_vlans_from_tag_string(row[8]))

    try:
        ordered = sorted(allowed, key=int)
    except ValueError:
        ordered = sorted(allowed)

    return entries, ordered


def _apply_uca_management_trunks(uca_rows: List[List[str]], *, include_mgmt_vlan: bool) -> Optional[List[str]]:
    if not include_mgmt_vlan:
        return None

    entries, allowed_list = _collect_trunk_allowed_from_rows(uca_rows, "UCA")
    if not entries:
        return None

    if "623" not in allowed_list:
        allowed_list.append("623")
    try:
        allowed_list = sorted(allowed_list, key=int)
    except ValueError:
        allowed_list = sorted(allowed_list)

    pos, last_row = max(
        entries,
        key=lambda entry: (_extract_if_index(entry[1][3]) or -1, entry[0]),
    )
    iface_new = last_row[3]
    sw_name = last_row[5]
    tag_str = f"VLANS={','.join(allowed_list)};ORIGIN=TRUNK"

    uca_rows[pos] = [
        "TRUNK",
        "TRUNK",
        "TRUNK UCA",
        iface_new,
        "TRUNK UCA",
        sw_name,
        "trunk",
        "trunk",
        tag_str,
        "N/A",
        "UCA",
    ]

    return allowed_list


def _ensure_poe_downlink_trunk(
    poe_rows: List[List[str]],
    allowed_vlans: Optional[List[str]],
) -> None:
    if not allowed_vlans:
        return

    try:
        ordered = sorted({str(v) for v in allowed_vlans}, key=int)
    except ValueError:
        ordered = sorted({str(v) for v in allowed_vlans})

    allowed_str = ",".join(ordered)
    tag_str = f"VLANS={allowed_str};ORIGIN=TRUNK"

    last_trunk_idx: Optional[int] = None
    last_trunk_sw: Optional[str] = None
    last_trunk_port: Optional[int] = None

    for idx, row in enumerate(poe_rows):
        if row[-1] != "POE":
            continue
        if str(row[0]).upper() != "TRUNK":
            continue
        last_trunk_idx = idx
        last_trunk_sw = row[5]
        last_trunk_port = _extract_if_index(row[3])

    candidate_idx: Optional[int] = None
    if last_trunk_idx is not None and last_trunk_sw is not None:
        best_port: Optional[int] = None
        best_idx: Optional[int] = None
        for idx, row in enumerate(poe_rows):
            if row[-1] != "POE":
                continue
            if str(row[0]).upper() != "LIBRE":
                continue
            if row[5] != last_trunk_sw:
                continue
            port = _extract_if_index(row[3])
            if port is None:
                continue
            if last_trunk_port is not None and port <= last_trunk_port:
                continue
            if best_port is None or port < best_port or (port == best_port and (best_idx is None or idx < best_idx)):
                best_port = port
                best_idx = idx
        if best_idx is not None:
            candidate_idx = best_idx

    if candidate_idx is None:
        for idx in range(len(poe_rows) - 1, -1, -1):
            row = poe_rows[idx]
            if row[-1] != "POE":
                continue
            if str(row[0]).upper() != "LIBRE":
                continue
            candidate_idx = idx
            break

    if candidate_idx is None:
        return

    iface_new = poe_rows[candidate_idx][3]
    sw_name = poe_rows[candidate_idx][5]
    poe_rows[candidate_idx] = [
        "TRUNK",
        "TRUNK",
        "TRUNK UCA",
        iface_new,
        "TRUNK UCA",
        sw_name,
        "trunk",
        "trunk",
        tag_str,
        "N/A",
        "POE",
    ]


def _relocate_poe_trunks(rows: List[List[str]]) -> None:
    """Move trunk entries to the last POE member so they end up at the tail ports."""

    trunk_rows: List[Tuple[List[str], int, int, int]] = []
    last_member: Optional[int] = None

    for idx, row in enumerate(rows):
        if not row or row[-1] != "POE":
            continue
        ifname = row[3] if len(row) > 3 else None
        member_idx = _extract_member_index(ifname) if isinstance(ifname, str) else None
        port_idx = _extract_if_index(ifname) if isinstance(ifname, str) else None
        if member_idx is not None and (last_member is None or member_idx > last_member):
            last_member = member_idx
        origin = _origin_from_tag_string(row[8]) if len(row) > 8 else None
        row_label = row[0] if row else ""
        is_trunk = False
        if isinstance(row_label, str) and row_label.upper() == "TRUNK":
            is_trunk = True
        elif origin == "TRUNK":
            is_trunk = True
        if is_trunk and member_idx is not None and port_idx is not None:
            trunk_rows.append((list(row), idx, member_idx, port_idx))

    if not trunk_rows or last_member is None:
        return

    candidate_slots: List[Tuple[int, int]] = []
    for idx, row in enumerate(rows):
        if not row or row[-1] != "POE":
            continue
        ifname = row[3] if len(row) > 3 else None
        member_idx = _extract_member_index(ifname) if isinstance(ifname, str) else None
        if member_idx != last_member:
            continue
        row_label = row[0] if row else ""
        if not (isinstance(row_label, str) and row_label.upper() == "LIBRE"):
            continue
        port_idx = _extract_if_index(ifname) if isinstance(ifname, str) else None
        if port_idx is None:
            continue
        candidate_slots.append((port_idx, idx))

    if len(candidate_slots) < len(trunk_rows):
        return

    candidate_slots.sort(reverse=True)

    for row_copy, original_idx, member_idx, port_idx in trunk_rows:
        formatter = _poe_interface_formatter(member_idx)
        sw_name = row_copy[5]
        rows[original_idx] = _libre_row(formatter, port_idx, sw_name, "POE")

    last_formatter = _poe_interface_formatter(last_member)
    last_sw_name = _poe_switch_name_for_member(last_member)

    for (row_copy, _, _, _), (port_idx, slot_idx) in zip(trunk_rows, candidate_slots):
        row_copy[3] = _resolve_new_interface(last_formatter, port_idx)
        row_copy[5] = last_sw_name
        rows[slot_idx] = row_copy


def make_mapping_video(video_logs: List[str]) -> Tuple[List[List[str]], Dict[Tuple[str, str], List[str]], Dict[str, Dict[str, Any]]]:
    rows: List[List[str]] = []
    iface_cfgs: Dict[Tuple[str, str], List[str]] = {}
    metadata_map: Dict[str, Dict[str, Any]] = {}

    for fp in video_logs:
        host, int_rows, _mac_map, _voice_map, iface_cfg_map, _last_io_map, metadata = parse_log(fp)

        metadata_map[host] = metadata

        for if_short, block in iface_cfg_map.items():
            iface_cfgs[(host, if_short)] = block[:]

        description_map = {
            if_short: _description_from_block(block)
            for if_short, block in iface_cfg_map.items()
        }

        with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
        order = parse_video_interface_order(lines)
        if not order:
            order = sorted(iface_cfg_map.keys(), key=_stack_interface_sort_key)

        sw_name = NEW_SWITCH_VIDEO_NAME
        new_idx = 1
        for if_src in order:
            desc = description_map.get(if_src)
            if desc is None:
                desc = next(
                    (
                        row.get("name")
                        for row in int_rows
                        if to_short_ifname(row["port"]) == if_src and row.get("name")
                    ),
                    None,
                )
            if not desc:
                desc = "N/A"
            new_if = _resolve_new_interface(NEW_IF_VIDEO_PREFIX, new_idx)
            new_idx += 1
            rows.append([
                host,
                if_src,
                desc,
                new_if,
                desc,
                sw_name,
                "ROUTED",
                "l3",
                "N/A",
                "N/A",
                "VIDEO",
            ])

    return rows, iface_cfgs, metadata_map

def _extract_if_index(ifname: str) -> Optional[int]:
    m = re.search(r"(\d+)$", ifname)
    return int(m.group(1)) if m else None


def _extract_member_index(ifname: str) -> Optional[int]:
    m = re.match(
        r"(?:TwoGigabitEthernet|TenGigabitEthernet|GigabitEthernet|TwentyFiveGigE)(\d+)",
        ifname,
        re.IGNORECASE,
    )
    return int(m.group(1)) if m else None


def _sort_group_rows(rows: List[List[str]]) -> None:
    rows.sort(
        key=lambda r: (
            r[5],
            _extract_member_index(r[3]) or 0,
            _extract_if_index(r[3]) or 0,
        )
    )

# ---------- Excel ----------

def export_excel(all_rows, out_dir, switch_number: Union[str, int]):
    switch_suffix = str(switch_number).strip()
    if not switch_suffix:
        switch_suffix = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    xlsx = os.path.join(out_dir, f"{switch_suffix}_Excel.xlsx")

    cols = [
        "SW Actual","Interface actual","Description actual","Interface nuevo",
        "Description nueva","SW Nuevo","VLAN","Mode","Tags","MAC Actual","_Grupo"
    ]
    df = pd.DataFrame(all_rows, columns=cols)

    sheet_defs = [
        ("AMBAR_POE", "POE"),
        ("AMBAR_T", "AMBAR_T"),
        ("UCA", "UCA"),
        ("VIDEO", "VIDEO"),
    ]

    with pd.ExcelWriter(xlsx, engine="xlsxwriter") as w:
        wb = w.book
        fmt_header = wb.add_format({'bold': True})
        fmt_libre  = wb.add_format({'bg_color': '#C8E6C9'})
        fmt_trunk  = wb.add_format({'bg_color': '#FFE0B2'})
        # Formatos para override por VLAN
        fmt_red = wb.add_format({'bg_color': '#FFCDD2'})  # rojo claro
        fmt_yellow = wb.add_format({'bg_color': '#FFF9C4'})  # amarillo claro

        vlan_col_format = wb.add_format({'num_format': '@'})
        palette = ['#E3F2FD', '#FCE4EC', '#E8F5E9', '#FFF3E0', '#EDE7F6', '#F1F8E9', '#E0F7FA']
        color_format_cache: Dict[str, Any] = {}

        for sheet_name, group_tag in sheet_defs:
            subset = df[df["_Grupo"] == group_tag]
            if subset.empty:
                continue

            sheet_df = subset.copy()

            display_map: Dict[str, str] = {}
            if group_tag == "POE":
                meta_by_name = {
                    str(info.get("sw_name")): info for info in POE_MEMBER_METADATA.values()
                }
                for sw_name, grp in subset.groupby("SW Nuevo"):
                    if grp.empty:
                        continue
                    interfaces = grp["Interface nuevo"].tolist()
                    tags = grp["Tags"].tolist()
                    display_map[sw_name] = _poe_display_label(
                        sw_name,
                        interfaces=interfaces,
                        tags=tags,
                        meta_by_name=meta_by_name,
                    )
            elif group_tag == "AMBAR_T":
                for sw_name, grp in subset.groupby("SW Nuevo"):
                    if grp.empty:
                        continue
                    try:
                        max_port = grp["Interface nuevo"].map(_extract_if_index).max()
                    except Exception:
                        max_port = None
                    label_size = 24 if max_port and max_port <= 24 else 48
                    display_map[sw_name] = f"C9300-{label_size}T"
            elif group_tag == "UCA":
                for sw_name, grp in subset.groupby("SW Nuevo"):
                    if grp.empty:
                        continue
                    try:
                        max_port = grp["Interface nuevo"].map(_extract_if_index).max()
                    except Exception:
                        max_port = None
                    label_size = 24 if max_port and max_port <= 24 else 48
                    display_map[sw_name] = f"C9300-{label_size}T"
            elif group_tag == "VIDEO":
                for sw_name, grp in subset.groupby("SW Nuevo"):
                    if grp.empty:
                        continue
                    interfaces = grp["Interface nuevo"].tolist()
                    display_map[sw_name] = _video_display_label(interfaces)

            if display_map:
                sheet_df["SW Nuevo"] = sheet_df["SW Nuevo"].map(lambda v: display_map.get(v, v))

            sheet_df.to_excel(w, sheet_name=sheet_name, index=False)
            ws = w.sheets[sheet_name]

            ws.set_row(0, None, fmt_header)
            ws.set_column("G:G", None, vlan_col_format)

            sw_actual_list = sheet_df["SW Actual"].tolist()
            sw_new_list = sheet_df["SW Nuevo"].tolist()
            mode_list = sheet_df["Mode"].tolist()
            # Lista de VLAN (string) para el override por VLAN
            vlan_list = [str(v) if v is not None else "" for v in sheet_df["VLAN"].tolist()]

            unique_switches = list(dict.fromkeys(sw_new_list))
            switch_formats: Dict[str, Any] = {}
            for idx_sw, sw in enumerate(unique_switches):
                if not sw:
                    continue
                color = palette[idx_sw % len(palette)]
                fmt = color_format_cache.get(color)
                if fmt is None:
                    fmt = wb.add_format({'bg_color': color})
                    color_format_cache[color] = fmt
                switch_formats[sw] = fmt

            for row_idx in range(1, len(subset) + 1):
                sw_actual = sw_actual_list[row_idx - 1]
                if str(sw_actual).upper() == "LIBRE":
                    ws.set_row(row_idx, None, fmt_libre)
                else:
                    mode_value = (mode_list[row_idx - 1] or "").lower() if row_idx - 1 < len(mode_list) else ""
                    if mode_value == "trunk":
                        ws.set_row(row_idx, None, fmt_trunk)
                    else:
                        sw_new = sw_new_list[row_idx - 1]
                        fmt = switch_formats.get(sw_new)
                        if fmt:
                            ws.set_row(row_idx, None, fmt)

            # --- OVERRIDE por VLAN (aplica encima de lo anterior) ---
            for row_idx in range(1, len(subset) + 1):
                vlan_val = vlan_list[row_idx - 1]
                if vlan_val in RED_VLANS:
                    ws.set_row(row_idx, None, fmt_red)
                elif vlan_val in YELLOW_VLANS:
                    ws.set_row(row_idx, None, fmt_yellow)

            col_index = sheet_df.columns.get_loc("_Grupo")
            ws.set_column(col_index, col_index, None, None, {'hidden': True})


    return xlsx


def export_removed_ports_report(
    skipped_ports: List[Dict[str, Any]],
    out_dir: str,
    switch_number: Union[str, int],
) -> str:
    switch_suffix = str(switch_number).strip() or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    txt_path = os.path.join(out_dir, f"{switch_suffix}_Puertos_Eliminados.txt")

    reason_text_map = {
        "status_not_connected": "Estado distinto de 'connected' sin actividad reciente.",
        "never": "La interfaz reporta 'Last input/Output never'.",
        "inactive_threshold": (
            "Inactividad superior a aproximadamente "
            f"{INACTIVITY_THRESHOLD_SECONDS // 86400} días."
        ),
    }

    with open(txt_path, "w", encoding="utf-8") as f:
        if not skipped_ports:
            f.write("No se eliminaron puertos por inactividad o desconexión.\n")
            return txt_path

        f.write("Puertos descartados por inactividad o estado desconectado:\n\n")
        for entry in skipped_ports:
            hostname = entry.get("hostname") or entry.get("host") or "N/A"
            port = entry.get("port") or "N/A"
            status = entry.get("status") or "N/A"
            alias = entry.get("name") or ""
            last_raw = entry.get("last_raw") or "N/D"
            output_raw = entry.get("output_raw") or "N/D"
            last_seconds = entry.get("last_seconds")
            reason_code = entry.get("reason") or ""
            reason_text = reason_text_map.get(reason_code, reason_code)

            f.write(f"Hostname origen: {hostname}\n")
            f.write(f"Puerto: {port}\n")
            if alias:
                f.write(f"  Alias (show int status): {alias}\n")
            f.write(f"  Estado: {status}\n")
            f.write(f"  Last input: {last_raw}\n")
            f.write(f"  Output: {output_raw}\n")
            if isinstance(last_seconds, (int, float)) and last_seconds:
                days = last_seconds / 86400
                f.write(f"  Inactividad aproximada: {days:.1f} días\n")
            if reason_text:
                f.write(f"  Motivo: {reason_text}\n")
            f.write("\n")

    return txt_path


# ---------- Plantillas base + export de configs ----------

def pick_existing_path(name: str):
    def variants(base: str):
        base = base.strip()
        base_nfc = unicodedata.normalize("NFC", base)
        base_nfd = unicodedata.normalize("NFD", base)
        swap_dash = base.replace("\u2013", "-").replace("\u2014", "-")
        with_dash = base.replace("-", "\u2013")
        return list(dict.fromkeys([base, base_nfc, base_nfd, swap_dash, with_dash]))

    search_dirs = ["", "templates", "shrunActual", "/mnt/data"]
    for v in variants(name):
        if os.path.isabs(v) and os.path.exists(v):
            return v
        for d in search_dirs:
            path = os.path.join(d, v) if d else v
            if os.path.exists(path):
                return path
    return None

AMBAR_TEMPLATE_PATH       = pick_existing_path(AMBAR_TEMPLATE_DEFAULT)       or AMBAR_TEMPLATE_DEFAULT
UCA_TEMPLATE_PATH         = pick_existing_path(UCA_TEMPLATE_DEFAULT)         or UCA_TEMPLATE_DEFAULT
AMBAR_EXTRA_TEMPLATE_PATH = pick_existing_path(AMBAR_EXTRA_TEMPLATE_DEFAULT) or AMBAR_EXTRA_TEMPLATE_DEFAULT
UCA_EXTRA_TEMPLATE_PATH   = pick_existing_path(UCA_EXTRA_TEMPLATE_DEFAULT)   or UCA_EXTRA_TEMPLATE_DEFAULT

VIDEO_ACL_LINES = [
    "access-list 10 permit 89.1.7.153",
    "access-list 10 remark Gestion_VIDEO",
    "access-list 10 permit 10.192.128.0 0.0.255.255",
    "access-list 10 permit 104.16.0.0 0.0.255.255",
    "access-list 10 permit 104.8.20.0 0.0.0.255",
    "access-list 10 permit 104.1.0.0 0.0.255.255",
    "access-list 10 permit 10.192.132.0 0.0.1.255",
    "access-list 10 permit 172.24.3.0 0.0.0.255",
    "access-list 10 permit 172.24.5.0 0.0.0.255",
    "access-list 10 permit 172.24.32.0 0.0.0.255",
    "access-list 10 permit 172.24.37.0 0.0.0.255",
    "access-list 10 permit 104.192.128.0 0.0.255.255",
]

VIDEO_BANNER_LINES = [
    "banner exec ^C",
    "Session established to $(hostname) on line $(line)",
    "^C",
    "banner incoming ^C",
    "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! PROHIBIDO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
    "!!                                                                            !!",
    "!!           Queda totalmente prohibido el uso del protocolo TELNET           !!",
    "!!                   Contacte con el administrador de la red                  !!",
    "!!                                                                            !!",
    "!!           The use of the TELNET protocol is completely prohibited          !!",
    "!!                       Contact the network administrator                    !!",
    "!!                                                                            !!",
    "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! FORBIDDEN !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
    "^C",
    "banner login ^C",
    "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! ATENCION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
    "!!                                                                            !!",
    "!!                          Solo personal autorizado                          !!",
    "!!                                                                            !!",
    "!!                          Authorized personal only                          !!",
    "!!                                                                            !!",
    "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! CAUTION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
    "^C",
    "banner motd ^C",
    "********************************************************************************",
    "**                                                                            **",
    "**                       AENA - Aeropuerto Barcelona                          **",
    "**                                                                            **",
    "**         Division de Tecnologias de la Informacion y Comunicaciones         **",
    "**                                                                            **",
    "**                  Esta accediendo a un sistema protegido                    **",
    "**          Si no esta autorizado cierre inmediatamente su conexion           **",
    "**                  La manipulacion no autorizada infringe                    **",
    "**             la ley 21/2003, de 7 de Julio, de Seguridad Aerea              **",
    "**                                                                            **",
    "********************************************************************************",
    "**                                                                            **",
    "**                         This is a Private System                           **",
    "**          If you are not authorized close your connection inmediatly        **",
    "**                    Unauthorized access is regulated by                     **",
    "**                   Air Security Law 21/2003, 7th of July                    **",
    "**                                                                            **",
    "********************************************************************************",
    "|                          Informacion de Acceso",
    "|                          Equipo: $(hostname)",
    "|",
    "|                       Autorizacion mediante TACACS+",
    "|",
    "^C",
    "banner prompt-timeout ^C",
    "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! ADVERTENCIA !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
    "!!                                                                            !!",
    "!!                      Ha experiado el tiempo de session                     !!",
    "!!                                                                            !!",
    "!!                        Has experienced session time                        !!",
    "!!                                                                            !!",
    "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! WARNING !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
    "^C",
    "banner config-save ^C",
    "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! INFORMACION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
    "!!                                                                            !!",
    "!!         Desea realizar una copia de la configuracion en ejecucion          !!",
    "!!                                                                            !!",
    "!!           You want to make a copy of the running configuration             !!",
    "!!                                                                            !!",
    "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! INFORMATION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
    "^C",
    "!",
    "line con 0",
    " session-timeout 15",
    " exec-timeout 15 0",
    " authorization exec TAC-AUTO",
    " accounting exec TAC-ACC",
    " logging synchronous",
    " login authentication TAC-AUTH",
    " stopbits 1",
    "line vty 0 4",
    " session-timeout 15",
    " exec-timeout 15 0",
    " authorization exec TAC-AUTO",
    " accounting exec TAC-ACC",
    " logging synchronous",
    " login authentication TAC-AUTH",
    " transport preferred ssh",
    "line vty 5 15",
    " session-timeout 15",
    " exec-timeout 15 0",
    " authorization exec TAC-AUTO",
    " accounting exec TAC-ACC",
    " logging synchronous",
    " login authentication TAC-AUTH",
    " transport preferred ssh",
    "!",
]

def _read_template_file(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            ln = raw.rstrip("\n")
            if STICKY_LINE_RE.search(ln):
                continue
            if BASE_HOSTNAME_RE.match(ln):
                continue
            if ln.strip().lower() == "end":
                continue
            out.append(ln)
    return out

def _chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]

def _ambar_extra_base_lines() -> List[str]:
    lines = [
        "clock timezone GMT 1 0",
        "clock summer-time GMT recurring 1 Sun Mar 2:00 last Sun Oct 2:00",
        "!",
        "aaa new-model",
        "!",
        "aaa group server tacacs+ ISE_GROUP",
        " server name ISE",
        " server name ISE2",
        "!",
        "service password-encryption",
        "!",
        "aaa authentication username-prompt LOCAL_username:",
        "aaa authentication password-prompt LOCAL_password:",
        "aaa authentication login TAC-AUTH group tacacs+ local",
        "aaa authorization config-commands",
        "aaa authorization exec TAC-AUTO group tacacs+ local if-authenticated",
        "aaa authorization commands 0 default group tacacs+ local",
        "aaa authorization commands 1 default group tacacs+ local",
        "aaa authorization commands 15 default group tacacs+ local",
        "aaa accounting exec TAC-ACC start-stop group tacacs+",
        "aaa accounting commands 0 default start-stop group tacacs+",
        "aaa accounting commands 1 default start-stop group tacacs+",
        "aaa accounting commands 15 default start-stop group tacacs+",
        "!",
        "aaa session-id common",
        "!",
        "no ip domain lookup",
        "vtp domain AMBAR",
        "vtp mode transparent",
        "ip domain name aena.es",
        "!",
        "login block-for 30 attempts 10 within 60",
        "!",
        "license smart url cslu https://104.16.0.102/cslu/v1/pi/BcnSSC-1",
        "!",
        "logging trap debugging",
        "logging host 104.18.220.10",
        "!",
        "spanning-tree mode mst",
        "spanning-tree portfast default",
        "spanning-tree portfast bpduguard default",
        "spanning-tree extend system-id",
        "!",
        "spanning-tree mst configuration",
        " name AMBAR",
        " revision 1",
    ]

    for chunk in _chunked(list(range(1, 4094, 2)), 12):
        lines.append(" instance 1 vlan " + ", ".join(str(n) for n in chunk))
    for chunk in _chunked(list(range(2, 4095, 2)), 12):
        lines.append(" instance 2 vlan " + ", ".join(str(n) for n in chunk))

    lines.extend([
        "!",
        "spanning-tree mst hello-time 1",
        "spanning-tree mst forward-time 4",
        "spanning-tree mst max-age 6",
        "spanning-tree mst max-hops 4",
        "!",
        "interface GigabitEthernet0/0",
        " vrf forwarding Mgmt-vrf",
        " ip address 6.6.6.6 255.255.255.252",
        " negotiation auto",
        "no shutdown",
        "!",
        "lldp run",
        "!",
        "ip ssh time-out 60",
        "ip ssh authentication-retries 2",
        "ip ssh version 2",
        "ip scp server enable",
        "!",
        "archive",
        " log config",
        "  logging enable",
        "  logging size 1000",
        "  notify syslog contenttype plaintext",
        "  hidekeys",
        "memory free low-watermark processor 79475",
        "!",
        "enable secret 0 Aena.2025!",
        "!",
        "username admin privilege 15 secret 0 4BCN@ena.2024,",
        "username NAC privilege 7 secret 9 $14$vVH7$Ezprg0JsSnaXVU$mxbty5BUxkUggfGwRM.D8TUnrhy2dgbt.i53OjvP6GQ",
        "!",
        "!",
        "ip default-gateway 10.192.130.254",
        "ip forward-protocol nd",
        "no ip http server",
        "ip http authentication aaa login-authentication TAC-AUTH",
        "ip http authentication aaa exec-authorization TAC-AUTO",
        "ip http secure-server",
        "ip tftp blocksize 512",
        "ip ssh time-out 60",
        "ip ssh authentication-retries 2",
        "ip ssh version 2",
        "ip scp server enable",
        "!",
        "!",
        "logging trap debugging",
        "logging host 4.9.0.135",
        "logging host 104.16.0.225",
        "logging host 104.18.220.10",
        "!",
        "tacacs server ISE",
        " address ipv4 104.16.0.210",
        " key 0 @Amb@r.2024!",
        "tacacs server ISE2",
        " address ipv4 104.16.0.211",
        " key 0 @Amb@r.2024!",
        "!",
        "snmp-server group V3gesred v3 priv notify *tv.FFFFFFFF.FFFFFFFF.FFFFFFFF.FFFFFFFF7F",
        "snmp-server group nacgroup v3 auth read nacview write nacview",
        "snmp-server group nacgroup v3 auth context AO-SIPAS_SVC2 read nacview write nacview",
        "snmp-server group nacgroup v3 auth context AO-SIPAS_SVC3 read nacview write nacview",
        "snmp-server group V3groupDCOM v3 priv read V3bcnAMBro write V3bcnAMBrw",
        "snmp-server view V3bcnAMBro iso included",
        "snmp-server view V3bcnAMBrw iso included",
        "snmp-server view nacview iso included",
        "snmp-server view V3GreBCNro iso included",
        "snmp-server view NO_BAD_SNMP iso included",
        "snmp-server view NO_BAD_SNMP internet included",
        "snmp-server view NO_BAD_SNMP snmpUsmMIB excluded",
        "snmp-server view NO_BAD_SNMP snmpVacmMIB excluded",
        "snmp-server view NO_BAD_SNMP snmpCommunityMIB excluded",
        "snmp-server view NO_BAD_SNMP transmission.94 excluded",
        "snmp-server view NO_BAD_SNMP mib-2.34.9 excluded",
        "snmp-server view NO_BAD_SNMP ciscoMgmt.35 excluded",
        "snmp-server view NO_BAD_SNMP ciscoMgmt.95 excluded",
        "snmp-server view NO_BAD_SNMP ciscoMgmt.130 excluded",
        "snmp-server view NO_BAD_SNMP ciscoMgmt.219 excluded",
        "snmp-server view NO_BAD_SNMP ciscoMgmt.252 excluded",
        "snmp-server view NO_BAD_SNMP ciscoMgmt.254 excluded",
        "snmp-server view NO_BAD_SNMP ciscoExperiment.997 excluded",
        "snmp-server community V3bcnAMBro RO",
        "snmp-server community V3bcnAMBrw RW",
        "snmp-server user V3Dcom V3groupDCOM v3 auth sha #2024@En@! priv des @En@,.2025!",
        "snmp-server community GreBCNro RO",
        "snmp-server community BCN2010rw RW",
        "snmp-server enable traps",
        "snmp-server host 104.16.0.225 version 2c GreBCNro",
        "snmp-server host 104.16.0.225 version 3 priv V3gesred",
        "snmp-server host 4.9.0.135 version 2c GreBCNro",
        "snmp-server host 4.9.0.135 version 3 priv V3gesred",
        "snmp-server host 104.18.220.10 version 2c GreBCNro",
        "snmp-server host 104.18.220.10 version 3 priv V3gesred",
        "!",
        "banner exec ^CC",
        "Session established to $(hostname) on line $(line)",
        "^C",
        "banner incoming ^CC",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! PROHIBIDO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "!!                                                                            !!",
        "!!           Queda totalmente prohibido el uso del protocolo TELNET           !!",
        "!!                   Contacte con el administrador de la red                  !!",
        "!!                                                                            !!",
        "!!           The use of the TELNET protocol is completely prohibited          !!",
        "!!                       Contact the network administrator                    !!",
        "!!                                                                            !!",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! FORBIDDEN !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "^C",
        "banner login ^CC",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! ATENCION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "!!                                                                            !!",
        "!!                          Solo personal autorizado                          !!",
        "!!                                                                            !!",
        "!!                          Authorized personal only                          !!",
        "!!                                                                            !!",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! CAUTION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "^C",
        "banner motd ^CC",
        "********************************************************************************",
        "**                                                                            **",
        "**                       AENA - Aeropuerto Barcelona                          **",
        "**                                                                            **",
        "**         Division de Tecnologias de la Informacion y Comunicaciones         **",
        "**                                                                            **",
        "**                  Esta accediendo a un sistema protegido                    **",
        "**          Si no esta autorizado cierre inmediatamente su conexion           **",
        "**                  La manipulacion no autorizada infringe                    **",
        "**             la ley 21/2003, de 7 de Julio, de Seguridad Aerea              **",
        "**                                                                            **",
        "********************************************************************************",
        "**                                                                            **",
        "**                         This is a Private System                           **",
        "**          If you are not authorized close your connection inmediatly        **",
        "**                    Unauthorized access is regulated by                     **",
        "**                   Air Security Law 21/2003, 7th of July                    **",
        "**                                                                            **",
        "********************************************************************************",
        "|                          Informacion de Acceso",
        "|                          Equipo: $(hostname)",
        "|",
        "|                       Autorizacion mediante TACACS+",
        "|",
        "^C",
        "banner prompt-timeout ^CC",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! ADVERTENCIA !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "!!                                                                            !!",
        "!!                      Ha experiado el tiempo de session                     !!",
        "!!                                                                            !!",
        "!!                        Has experienced session time                        !!",
        "!!                                                                            !!",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! WARNING !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "^C",
        "banner config-save ^CC",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! INFORMACION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "!!                                                                            !!",
        "!!         Desea realizar una copia de la configuracion en ejecucion          !!",
        "!!                                                                            !!",
        "!!           You want to make a copy of the running configuration             !!",
        "!!                                                                            !!",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! INFORMATION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "^C",
        "!",
        "!",
        "call-home",
        " ! If contact email address in call-home is configured as sch-smart-licensing@cisco.com",
        " ! the email address configured in Cisco Smart License Portal will be used as contact email address to send SCH notifications.",
        " contact-email-addr sch-smart-licensing@cisco.com",
        " no http secure server-identity-check",
        " profile \"CiscoTAC-1\"",
        "  active",
        "  destination transport-method http",
        "  no destination address http https://tools.cisco.com/its/service/oddce/services/DDCEService ",
        " profile \"BCNscc\"",
        "  destination transport-method http",
        "  destination address http https://104.16.0.102:443/Transportgateway/services/DeviceRequestHandler ",
        "!",
        "line con 0",
        " session-timeout 15",
        " exec-timeout 15 0",
        " authorization exec TAC-AUTO",
        " accounting exec TAC-ACC",
        " logging synchronous",
        " login authentication TAC-AUTH",
        " stopbits 1",
        "line vty 0 4",
        " session-timeout 15",
        " exec-timeout 15 0",
        " authorization exec TAC-AUTO",
        " accounting exec TAC-ACC",
        " logging synchronous",
        " login authentication TAC-AUTH",
        " transport preferred ssh",
        "line vty 5 15",
        " session-timeout 15",
        " exec-timeout 15 0",
        " authorization exec TAC-AUTO",
        " accounting exec TAC-ACC",
        " logging synchronous",
        " login authentication TAC-AUTH",
        " transport preferred ssh",
        "!",
        "ntp server 104.253.1.1",
        "ntp server 104.253.1.2",
        "!",
        "access-list 10 permit 89.1.7.153",
        "access-list 10 remark Gestion",
        "access-list 10 permit 104.16.0.0 0.0.255.255",
        "access-list 10 permit 104.1.0.0 0.0.255.255",
        "access-list 10 permit 172.24.3.0 0.0.0.255",
        "access-list 10 permit 172.24.5.0 0.0.0.255",
        "access-list 10 permit 172.24.32.0 0.0.0.255",
        "access-list 10 permit 172.24.37.0 0.0.0.255",
        "access-list 10 permit 104.1.0.0 0.0.255.255",
        "access-list 10 permit 104.192.128.0 0.0.255.255",
        "!",
        "! -------------------------------------------------------",
        "! (Comentarios sobre troncales ignorados por esta versión)",
        "! -------------------------------------------------------",
    ])
    return lines


def _uca_extra_base_lines() -> List[str]:
    lines = [
        "aaa new-model",
        "aaa group server tacacs+ ISE_GROUP",
        " server name ISE",
        " server name ISE2",
        "aaa authentication username-prompt LOCAL_username:",
        "aaa authentication password-prompt LOCAL_password:",
        "aaa authentication login TAC-AUTH group tacacs+ local",
        "aaa authorization config-commands",
        "aaa authorization exec TAC-AUTO group tacacs+ local if-authenticated",
        "aaa authorization commands 0 default group tacacs+ local",
        "aaa authorization commands 1 default group tacacs+ local",
        "aaa authorization commands 15 default group tacacs+ local",
        "aaa accounting exec TAC-ACC start-stop group tacacs+",
        "aaa accounting commands 0 default start-stop group tacacs+",
        "aaa accounting commands 1 default start-stop group tacacs+",
        "aaa accounting commands 15 default start-stop group tacacs+",
        "aaa session-id common",
        "!",
        "service password-encryption",
        "!",
        "vtp domain UCA",
        "vtp mode transparent",
        "ip domain name aena.es",
        "!",
        "login block-for 30 attempts 10 within 60",
        "!",
        "spanning-tree mode mst",
        "spanning-tree portfast default",
        "spanning-tree portfast bpduguard default",
        "spanning-tree extend system-id",
        "",
        "spanning-tree mst configuration",
        " name UCA",
        " revision 1",
        " instance 1 vlan 141, 2205, 2207, 2209, 2211, 2213, 2215, 2217, 2219, 2221",
        " instance 1 vlan 2223, 2225, 2227, 2229",
        " instance 2 vlan 32, 110, 142, 2204, 2206, 2208, 2210, 2212, 2214, 2216",
        " instance 2 vlan 2218, 2220, 2222, 2224, 2226, 2228, 2230, 2300",
        "",
        "spanning-tree mst hello-time 1",
        "spanning-tree mst forward-time 4",
        "spanning-tree mst max-age 6",
        "!",
        "lldp run",
        "!",
        "vlan 2230",
        "name AO-GreBCNUCA",
        "!",
        "interface GigabitEthernet0/0",
        " vrf forwarding Mgmt-vrf",
        " ip address 6.6.6.6 255.255.255.252",
        " negotiation auto",
        "no shutdown",
        "!",
        "interface vlan2230",
        " ip address 10.192.129.x 255.255.255.0",
        "!",
        "ip tftp source-interface vlan2230",
        "ip ssh time-out 60",
        "ip ssh authentication-retries 2",
        "ip ssh version 2",
        "ip scp server enable",
        "!",
        "archive",
        " log config",
        "  logging enable",
        "  logging size 1000",
        "  notify syslog contenttype plaintext",
        "memory free low-watermark processor 134148",
        "!",
        "username admin privilege 15 secret 0 0BCN@ena.2024,",
        "!",
        "!",
        "enable secret 0 Aena.2025!",
        "!",
        "!",
        "ip default-gateway 10.192.129.254",
        "no ip http server",
        "ip http authentication aaa login-authentication TAC-AUTH",
        "ip http authentication aaa exec-authorization TAC-AUTO",
        "ip http secure-server",
        "ip http client source-interface Vlan2230",
        "ip tftp source-interface Vlan2230",
        "ip tftp blocksize 512",
        "ip ssh time-out 60",
        "ip ssh authentication-retries 2",
        "ip ssh version 2",
        "ip scp server enable",
        "!",
        "logging trap debugging",
        "logging host 4.9.0.135",
        "logging host 104.16.0.225",
        "logging host 104.18.220.10",
        "!",
        "tacacs server ISE",
        " address ipv4 104.16.0.210",
        " key 0 @UC@.2024!",
        "tacacs server ISE2",
        " address ipv4 104.16.0.211",
        " key 0 @UC@.2024!",
        "!",
        "snmp-server group V3groupDCOM v3 priv notify *tv.FFFFFFFF.FFFFFFFF.FFFFFFFF.FFFFFFFF7F",
        "snmp-server group V3groupDCOM v3 priv read V3bcnVIDro write V3bcnVIDrw",
        "snmp-server view V3bcnVIDBro iso included",
        "snmp-server view V3bcnVIDrw iso included",
        "snmp-server view nacview iso included",
        "snmp-server view V3bcnVIDro iso included",
        "snmp-server community V3bcnVIDro RO",
        "snmp-server community V3bcnVIDrw RW",
        "snmp-server user V3Dcom V3groupDCOM v3 auth sha #2024@En@! priv des @En@,.2025!",
        "snmp-server enable traps",
        "snmp-server host 104.16.0.225 version 2c GreBCNro",
        "snmp-server host 104.16.0.225 version 3 priv V3gesred",
        "snmp-server host 4.9.0.135 version 2c GreBCNro",
        "snmp-server host 4.9.0.135 version 3 priv V3gesred",
        "snmp-server host 104.18.220.10 version 2c GreBCNro",
        "snmp-server host 104.18.220.10 version 3 priv V3gesred",
        "!",
        "banner exec ^C",
        "Session established to $(hostname) on line $(line)",
        "^C",
        "banner incoming ^C",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! PROHIBIDO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "!!                                                                            !!",
        "!!           Queda totalmente prohibido el uso del protocolo TELNET           !!",
        "!!                   Contacte con el administrador de la red                  !!",
        "!!                                                                            !!",
        "!!           The use of the TELNET protocol is completely prohibited          !!",
        "!!                       Contact the network administrator                    !!",
        "!!                                                                            !!",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! FORBIDDEN !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "^C",
        "banner login ^C",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! ATENCION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "!!                                                                            !!",
        "!!                          Solo personal autorizado                          !!",
        "!!                                                                            !!",
        "!!                          Authorized personal only                          !!",
        "!!                                                                            !!",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! CAUTION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "^C",
        "banner motd ^C",
        "********************************************************************************",
        "**                                                                            **",
        "**                       AENA - Aeropuerto Barcelona                          **",
        "**                                                                            **",
        "**         Division de Tecnologias de la Informacion y Comunicaciones         **",
        "**                                                                            **",
        "**                  Esta accediendo a un sistema protegido                    **",
        "**          Si no esta autorizado cierre inmediatamente su conexion           **",
        "**                  La manipulacion no autorizada infringe                    **",
        "**             la ley 21/2003, de 7 de Julio, de Seguridad Aerea              **",
        "**                                                                            **",
        "********************************************************************************",
        "**                                                                            **",
        "**                         This is a Private System                           **",
        "**          If you are not authorized close your connection inmediatly        **",
        "**                    Unauthorized access is regulated by                     **",
        "**                   Air Security Law 21/2003, 7th of July                    **",
        "**                                                                            **",
        "********************************************************************************",
        "|                          Informacion de Acceso",
        "|                          Equipo: $(hostname)",
        "|",
        "|                       Autorizacion mediante TACACS+",
        "|",
        "^C",
        "banner prompt-timeout ^C",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! ADVERTENCIA !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "!!                                                                            !!",
        "!!                      Ha experiado el tiempo de session                     !!",
        "!!                                                                            !!",
        "!!                        Has experienced session time                        !!",
        "!!                                                                            !!",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! WARNING !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "^C",
        "banner config-save ^C",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! INFORMACION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "!!                                                                            !!",
        "!!         Desea realizar una copia de la configuracion en ejecucion          !!",
        "!!                                                                            !!",
        "!!           You want to make a copy of the running configuration             !!",
        "!!                                                                            !!",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! INFORMATION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "^C",
        "",
        "line con 0",
        " session-timeout 15",
        " exec-timeout 15 0",
        " authorization exec TAC-AUTO",
        " accounting exec TAC-ACC",
        " logging synchronous",
        " login authentication TAC-AUTH",
        " stopbits 1",
        "line vty 0 4",
        " session-timeout 15",
        " exec-timeout 15 0",
        " authorization exec TAC-AUTO",
        " accounting exec TAC-ACC",
        " logging synchronous",
        " login authentication TAC-AUTH",
        " transport preferred ssh",
        "line vty 5 15",
        " session-timeout 15",
        " exec-timeout 15 0",
        " authorization exec TAC-AUTO",
        " accounting exec TAC-ACC",
        " logging synchronous",
        " login authentication TAC-AUTH",
        " transport preferred ssh",
        "",
        "ntp source vlan2230",
        "ntp server 104.253.1.1",
        "ntp server 104.253.1.2",
        "",
        "access-list 10 permit 89.1.7.153",
        "access-list 10 remark Gestion",
        "access-list 10 permit 104.16.0.0 0.0.255.255",
        "access-list 10 permit 10.192.129.0 0.0.0.255",
        "access-list 10 permit 104.1.0.0 0.0.255.255",
        "access-list 10 permit 104.129.0.0 0.0.255.255",
        "access-list 10 permit 104.241.0.0 0.0.255.255",
        "access-list 10 permit 104.242.0.0 0.0.255.255",
        "access-list 10 permit 172.24.3.0 0.0.0.255",
        "access-list 10 permit 172.24.5.0 0.0.0.255",
        "access-list 10 permit 172.24.32.0 0.0.0.255",
        "access-list 10 permit 172.24.37.0 0.0.0.255",
        "access-list 10 permit 104.192.128.0 0.0.255.255",
        "!",
        "vlan 2204",
        " name SipaSVC4",
        "!",
        "vlan 2205",
        " name SipaSVC5",
        "!",
        "vlan 2206",
        " name SipaSVC6",
        "!",
        "vlan 2207",
        " name SipaSVC7",
        "!",
        "vlan 2208",
        " name SipaSVC8",
        "!",
        "vlan 2209",
        " name UCA10",
        "!",
        "vlan 2210",
        " name UCA11",
        "!",
        "vlan 2211",
        " name UCA12",
        "!",
        "vlan 2212",
        " name UCA13",
        "!",
        "vlan 2215",
        " name AO_UCA_SBD_T1",
        "!",
        "vlan 2216",
        " name AO-SIPAV6-T1",
        "!",
        "vlan 2217",
        " name AO-SIPAV6-T1-Gen2",
        "!",
        "vlan 2218",
        " name AO-SIPAV6-T1-Emb1",
        "!",
        "vlan 2219",
        " name AO-SIPAV6-T1-Emb2",
        "!",
        "vlan 2221",
        " name AO-SIPAV6-T1-Fac1",
        "!",
        "vlan 2222",
        " name AO-SIPAV6-T1-Fac2",
        "!",
        "vlan 2300",
        " name GESTION_SIPA",
        "!",
        "! (Comentarios sobre troncales ignorados por esta versión)",
    ]
    return lines



def _video_extra_base_lines(metadata: Dict[str, Any], *, level: str) -> List[str]:
    def _short(name: Optional[str]) -> Optional[str]:
        return to_short_ifname(name) if name else None

    def _format_source_interface(name: str) -> str:
        if not name:
            return name
        lowered = name.lower()
        if lowered.startswith("loopback"):
            suffix = name[len("Loopback"):]
            if suffix.isdigit():
                return f"loopback {suffix}"
        return name

    level_key = str(level).strip()
    if level_key not in {"2", "3"}:
        level_key = "2"

    mgmt_candidate = _short(metadata.get("management_interface"))
    mgmt_vlan_candidate = _short(metadata.get("video_management_interface"))

    def _pick_level2_mgmt() -> str:
        for cand in (mgmt_candidate, mgmt_vlan_candidate):
            if cand and cand.lower().startswith("vlan9"):
                return cand
        for cand in (mgmt_candidate, mgmt_vlan_candidate):
            if cand and cand.lower().startswith("vlan"):
                return cand
        for cand in (mgmt_candidate, mgmt_vlan_candidate):
            if cand:
                return cand
        return "Vlan9XX"

    if level_key == "2":
        mgmt_if = _pick_level2_mgmt()
    else:
        mgmt_if = mgmt_candidate or "Loopback0"

    default_gateway = metadata.get("ip_default_gateway")
    if not default_gateway:
        default_gateway = "104.129.XX.XX" if level_key == "2" else "10.192.133.254"

    register_if = _short(metadata.get("ip_pim_register_source")) or mgmt_if
    rp_lines = [ln.strip() for ln in metadata.get("ip_pim_rp_lines", []) if ln.strip()]

    name_servers_raw = metadata.get("ip_name_server_lines") or ["ip name-server 104.16.99.100"]
    name_servers: List[str] = []
    for ns in name_servers_raw:
        ns_line = ns.strip()
        if ns_line and ns_line not in name_servers:
            name_servers.append(ns_line)

    tftp_if = _short(metadata.get("ip_tftp_source_interface")) or mgmt_if
    http_if = _short(metadata.get("ip_http_client_source_interface")) or mgmt_if
    snmp_src_if = _format_source_interface(_short(metadata.get("snmp_source_interface")) or mgmt_if)
    ntp_source_if = _short(metadata.get("ntp_source")) or mgmt_if

    lines: List[str] = [
        "!",
        "service password-encryption",
        "!",
        "aaa new-model",
        "aaa group server tacacs+ ISE_GROUP",
        " server name ISE",
        " server name ISE2",
        "aaa authentication username-prompt LOCAL_username:",
        "aaa authentication password-prompt LOCAL_password:",
        "aaa authentication login TAC-AUTH group tacacs+ local",
        "aaa authorization config-commands",
        "aaa authorization exec TAC-AUTO group tacacs+ local if-authenticated",
        "aaa authorization commands 0 default group tacacs+ local",
        "aaa authorization commands 1 default group tacacs+ local",
        "aaa authorization commands 15 default group tacacs+ local",
        "aaa accounting exec TAC-ACC start-stop group tacacs+",
        "aaa accounting commands 0 default start-stop group tacacs+",
        "aaa accounting commands 1 default start-stop group tacacs+",
        "aaa accounting commands 15 default start-stop group tacacs+",
        "aaa session-id common",
        "!",
    ]

    if level_key == "3":
        lines.extend([
            "ip routing",
            "ip multicast-routing",
            "ip multicast multipath",
            "!",
        ])
    else:
        lines.append("!")

    lines.extend(name_servers)
    lines.extend([
        "no ip domain lookup",
        "ip domain name aena.es",
        "!",
        "login block-for 30 attempts 10 within 60",
        "!",
        "lldp run",
        "!",
        f"ip tftp source-interface {tftp_if}",
        "ip ssh time-out 60",
        "ip ssh authentication-retries 2",
        "ip ssh version 2",
        "ip scp server enable",
        "!",
        "archive",
        " log config",
        "  logging enable",
        "  logging size 500",
        "  notify syslog contenttype plaintext",
        "memory free low-watermark processor 134148",
        "!",
        "enable secret 0 Aena.2025!",
        "!",
        "username admin privilege 15 secret 0 7BCN@ena.2024,",
        "!",
        "interface GigabitEthernet0/0",
        " vrf forwarding Mgmt-vrf",
        " ip address 6.6.6.6 255.255.255.252",
        " negotiation auto",
        "no shutdown",
        "!",
        "interface Loopback0",
    ])

    if level_key == "2":
        lines.extend([
            " ip address 10.192.132.x 255.255.255.255",
            "shutdown",
            "!",
            f"interface {mgmt_if}",
            " ip address 104.129.XX.YY 255.255.255.252",
            " no ip route-cache",
            "!",
        ])
    else:
        lines.extend([
            " ip address 10.192.132.11 255.255.255.255",
            "!",
        ])

    lines.append(f"ip default-gateway {default_gateway}")

    if level_key == "3":
        if rp_lines:
            lines.extend(rp_lines)
        else:
            lines.append("ip pim rp-address 104.241.254.254")

    lines.append(f"ip pim register-source {register_if}")

    if level_key == "2" and rp_lines:
        lines.extend(rp_lines)

    lines.extend([
        "ip forward-protocol nd",
        "no ip http server",
        "ip http authentication aaa login-authentication TAC-AUTH",
        "ip http authentication aaa exec-authorization TAC-AUTO",
        "ip http secure-server",
        f"ip http client source-interface {http_if}",
        f"ip tftp source-interface {tftp_if}",
        "ip tftp blocksize 512",
        "ip ssh time-out 60",
        "ip ssh authentication-retries 2",
        "ip ssh version 2",
        "ip scp server enable",
        "!",
        "logging trap debugging",
        "logging host 4.9.0.135",
        "logging host 104.16.0.225",
        "logging host 104.18.220.10",
        "!",
        "tacacs server ISE",
        " address ipv4 104.16.0.210",
        " key 0 @V1de0.2024!",
        "tacacs server ISE2",
        " address ipv4 104.16.0.211",
        " key 0 @V1de0.2024!",
        "!",
        "snmp-server group V3groupDCOM v3 priv notify *tv.FFFFFFFF.FFFFFFFF.FFFFFFFF.FFFFFFFF7F",
        "snmp-server group V3groupDCOM v3 priv read V3bcnVIDro write V3bcnVIDrw",
        "snmp-server view V3bcnVIDBro iso included",
        "snmp-server view V3bcnVIDrw iso included",
        "snmp-server view nacview iso included",
        "snmp-server view V3bcnVIDro iso included",
        "snmp-server community V3bcnVIDro RO",
        "snmp-server community V3bcnVIDrw RW",
        "snmp-server community GreBCNro RO",
        "snmp-server community BCN2010rw RW",
        "snmp-server user V3Dcom V3groupDCOM v3 auth sha #2024@En@! priv des @En@,.2025!",
        "snmp-server enable traps",
    ])

    if snmp_src_if:
        lines.append(f"snmp-server source-interface traps {snmp_src_if}")

    lines.extend([
        "snmp-server host 104.16.0.225 version 2c GreBCNro",
        "snmp-server host 104.16.0.225 version 3 priv V3gesred",
        "snmp-server host 4.9.0.135 version 2c GreBCNro",
        "snmp-server host 4.9.0.135 version 3 priv V3gesred",
        "snmp-server host 104.18.220.10 version 2c GreBCNro",
        "snmp-server host 104.18.220.10 version 3 priv V3gesred",
        "!",
    ])

    lines.extend(VIDEO_BANNER_LINES)

    if ntp_source_if:
        lines.append(f"ntp source {ntp_source_if}")
    lines.extend([
        "ntp server 104.253.1.1",
        "ntp server 104.253.1.2",
        "!",
    ])

    lines.extend(VIDEO_ACL_LINES)
    lines.append("!")

    router_blocks = metadata.get("router_blocks", [])
    for block in router_blocks:
        for ln in block:
            stripped = ln.strip()
            if stripped:
                lines.append(stripped)
        lines.append("!")

    return lines


def _collect_video_vlan_blocks(
    metadata_map: Dict[str, Dict[str, Any]],
    hosts: Iterable[str],
) -> List[List[str]]:
    seen_headers: Set[str] = set()
    blocks: List[List[str]] = []
    for host in hosts:
        meta = metadata_map.get(host) or {}
        for block in meta.get("vlan_blocks", []):
            if not block:
                continue
            header = block[0].strip()
            if not header.lower().startswith("vlan "):
                continue
            key = header.lower()
            if key in seen_headers:
                continue
            seen_headers.add(key)
            blocks.append(block)
    return blocks


def _collect_video_svi_blocks(
    iface_cfgs: Dict[Tuple[str, str], List[str]],
    hosts: Iterable[str],
) -> List[Tuple[str, List[str]]]:
    seen_interfaces: Set[str] = set()
    ordered_hosts = list(dict.fromkeys(hosts))
    svi_blocks: List[Tuple[str, List[str]]] = []
    for host in ordered_hosts:
        for (cfg_host, if_short), block in iface_cfgs.items():
            if cfg_host != host:
                continue
            short_name = to_short_ifname(if_short)
            if not short_name.lower().startswith("vlan"):
                continue
            key = (short_name or "").lower()
            if key in seen_interfaces:
                continue
            seen_interfaces.add(key)
            svi_blocks.append((short_name, block))
    return svi_blocks


def _emit_base_template(
    f,
    forced_hostname: str,
    which: str,
    *,
    include_vlan_623: bool = False,
    default_gateway_override: Optional[str] = None,
):
    def _override_default_gateway(line: str) -> str:
        if not default_gateway_override:
            return line

        match = DEFAULT_GATEWAY_LINE_RE.match(line)
        if not match:
            return line

        prefix, _, suffix = match.groups()
        return f"{prefix}{default_gateway_override}{suffix}"

    def _uca_vlan623_override(line: str) -> str:
        stripped = line.strip()
        lower = stripped.lower()
        if not lower:
            return line

        overrides = {
            "ip tftp source-interface vlan2230": "ip tftp source-interface Vlan623",
            "ip http client source-interface vlan2230": "ip http client source-interface Vlan623",
            "ntp source vlan2230": "ntp source Vlan623",
        }

        replacement = overrides.get(lower)
        if not replacement:
            return line

        prefix_len = len(line) - len(line.lstrip())
        return f"{line[:prefix_len]}{replacement}"

    if which in ("POE", "AMBAR_T"):
        base_lines = _read_template_file(AMBAR_TEMPLATE_PATH)
        base_name  = "AMBAR"
    elif which == "UCA":
        base_lines = _read_template_file(UCA_TEMPLATE_PATH)
        base_name  = "UCA"
    else:
        base_lines = []
        base_name  = "GENERIC"

    f.write(f"!\n! === BASE TEMPLATE: {base_name} ===\n")
    f.write(f"hostname {forced_hostname}\n")
    for ln in base_lines:
        if BASE_HOSTNAME_RE.match(ln):
            continue
        rewritten = _override_default_gateway(ln)
        f.write(rewritten + ("\n" if not rewritten.endswith("\n") else ""))

    if which in ("POE", "AMBAR_T"):
        extra_lines = _read_template_file(AMBAR_EXTRA_TEMPLATE_PATH)
        if not extra_lines:
            extra_lines = _ambar_extra_base_lines()
        if extra_lines:
            f.write("!\n! === CONFIG BASE AMBAR ADICIONAL ===\n")
            for ln in extra_lines:
                rewritten = _override_default_gateway(ln)
                f.write(rewritten + ("\n" if not rewritten.endswith("\n") else ""))
    elif which == "UCA":
        extra_lines = _read_template_file(UCA_EXTRA_TEMPLATE_PATH)
        if not extra_lines:
            extra_lines = _uca_extra_base_lines()
        if extra_lines:
            f.write("!\n! === CONFIG BASE UCA ADICIONAL ===\n")
            skip_vlan_block = False
            for ln in extra_lines:
                stripped = ln.strip()
                lower = stripped.lower()
                if lower.startswith("vlan "):
                    skip_vlan_block = True
                    continue
                if skip_vlan_block:
                    if not stripped or stripped == "!":
                        skip_vlan_block = False
                        continue
                    if lower.startswith("name "):
                        continue
                out_line = (
                    _uca_vlan623_override(ln)
                    if include_vlan_623
                    else ln
                )
                rewritten = _override_default_gateway(out_line)
                f.write(rewritten + ("\n" if not rewritten.endswith("\n") else ""))


def _filter_out_sticky(lines: List[str]) -> List[str]:
    """Remove sticky MAC entries while keeping the generic sticky command."""
    filtered: List[str] = []
    for ln in lines:
        if STICKY_LINE_RE.search(ln):
            if re.match(r"^\s*switchport\s+port-security\s+mac-address\s+sticky\s*$", ln, re.IGNORECASE):
                filtered.append(ln)
            else:
                # Descarta líneas que fijan direcciones MAC concretas.
                continue
        elif re.search(r"switchport\s+port-security\s+violation", ln, re.IGNORECASE):
            continue
        elif re.search(r"switchport\s+port-security\s+aging", ln, re.IGNORECASE):
            continue
        else:
            filtered.append(ln)
    return filtered

def _normalize_command(cmd: str) -> str:
    return re.sub(r"\s+", " ", cmd.strip().lower()) if cmd.strip() else ""

def _ensure_security_basics(commands: List[str], *, enable_port_security: bool = True) -> None:
    existing = {
        _normalize_command(cmd)
        for cmd in commands
        if cmd and not cmd.strip().startswith("!")
    }

    if enable_port_security:
        required = [
            " switchport port-security mac-address sticky",
            " switchport port-security",
            " spanning-tree portfast",
        ]
        for line in required:
            norm = _normalize_command(line)
            if norm and norm not in existing:
                commands.append(line)
                existing.add(norm)


def _collect_switch_locations(
    rows: List[List[str]],
    group_tag: str,
    host_metadata: Dict[str, Dict[str, Any]],
) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for row in rows:
        if row[-1] != group_tag:
            continue
        src_host = row[0]
        if not src_host or str(src_host).upper() == "LIBRE":
            continue
        meta = host_metadata.get(src_host)
        if not meta:
            continue
        location = meta.get("snmp_location")
        if location and row[5] not in mapping:
            mapping[row[5]] = location
    return mapping


def _collect_switch_contacts(
    rows: List[List[str]],
    group_tag: str,
    host_metadata: Dict[str, Dict[str, Any]],
) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for row in rows:
        if row[-1] != group_tag:
            continue
        src_host = row[0]
        if not src_host or str(src_host).upper() == "LIBRE":
            continue
        meta = host_metadata.get(src_host)
        if not meta:
            continue
        contact = meta.get("snmp_contact") or DEFAULT_SNMP_CONTACT
        if row[5] not in mapping:
            mapping[row[5]] = contact
    return mapping


def _collect_group_vlans(rows: List[List[str]], group_tag: str) -> Set[str]:
    vlans: Set[str] = set()
    for row in rows:
        if row[-1] != group_tag or row[0] == "LIBRE":
            continue
        vlan = row[6]
        if vlan and vlan.isdigit():
            vlans.add(vlan)
        vlans.update(_vlans_from_tag_string(row[8]))
    return {v for v in vlans if v and v.isdigit()}


def _poe_uplink_interfaces() -> List[Tuple[str, str]]:
    """Return the default uplink members for the POE (UXM/AMBAR) stack."""
    members = sorted(POE_MEMBER_METADATA.keys())
    first_member = members[0] if members else 1
    second_member = members[1] if len(members) > 1 else first_member + 1
    return [
        (f"TwentyFiveGigE{first_member}/1/1", "Interfaz Uplink CORE_1"),
        (f"TenGigabitEthernet{second_member}/1/1", "Interfaz Uplink CORE_2"),
    ]


def _ambar_t_uplink_interfaces() -> List[Tuple[str, str]]:
    """Uplinks for AMBAR-T stacks (fixed numbering)."""
    return [
        ("TwentyFiveGigE1/1/1", "Interfaz Uplink CORE_1"),
        ("TenGigabitEthernet2/1/1", "Interfaz Uplink CORE_2"),
    ]


def _uca_uplink_interfaces() -> List[Tuple[str, str]]:
    """Uplinks for UCA stacks (fixed numbering)."""
    return [
        ("TenGigabitEthernet1/1/1", "Interfaz Uplink CORE_1"),
        ("TenGigabitEthernet1/1/2", "Interfaz Uplink CORE_2"),
    ]


def _stack_interface_sort_key(iface: str) -> Tuple[int, Tuple[int, ...], int, str]:
    nums = [int(n) for n in re.findall(r"\d+", iface)]
    if not nums:
        return (10**6, (), 10**6, iface)
    member = nums[0]
    mid = tuple(nums[1:-1]) if len(nums) > 2 else ()
    port = nums[-1]
    return (member, mid, port, iface)


def _build_management_vlan_lines(
    rows_sorted: List[List[str]],
    iface_cfgs: Dict[Tuple[str, str], List[str]],
    host_metadata: Optional[Dict[str, Dict[str, Any]]],
    *,
    iface_name: str = "Vlan623",
    fallback_ip_line: Optional[str] = " ip address 10.192.130.35 255.255.254.0",
    default_gateway: Optional[str] = None,
) -> List[str]:
    if not rows_sorted:
        return []

    metadata_map = host_metadata or {}
    metadata: Dict[str, Any] = {}
    mgmt_block: Optional[List[str]] = None

    for row in rows_sorted:
        src_host = row[0]
        if not src_host or str(src_host).upper() == "LIBRE":
            continue
        metadata = metadata_map.get(src_host, {})
        mgmt_block = iface_cfgs.get((src_host, iface_name))
        if mgmt_block:
            break

    lines: List[str] = [f"interface {iface_name}"]
    iface_lines: List[str] = []

    if mgmt_block:
        for ln in mgmt_block:
            stripped = ln.rstrip("\n")
            if re.match(r"^\s*interface\b", stripped, re.IGNORECASE):
                continue
            iface_lines.append(stripped)

    if fallback_ip_line and not any(
        re.match(r"^\s*ip\s+address\b", ln, re.IGNORECASE) for ln in iface_lines
    ):
        iface_lines.insert(0, fallback_ip_line)

    has_shutdown = any(re.match(r"^\s*shutdown\b", ln, re.IGNORECASE) for ln in iface_lines)
    has_no_shutdown = any(re.match(r"^\s*no\s+shutdown\b", ln, re.IGNORECASE) for ln in iface_lines)
    if not has_shutdown and not has_no_shutdown:
        iface_lines.append(" no shutdown")

    lines.extend(iface_lines)
    lines.append("!")

    mgmt_if = iface_name
    default_gw = metadata.get("ip_default_gateway") if metadata else None
    if not default_gw:
        if default_gateway:
            default_gw = default_gateway
        else:
            default_gw = "10.192.130.254"

    lines.append(f"ip tftp source-interface {mgmt_if}")
    lines.append(f"ip http client source-interface {mgmt_if}")
    lines.append(f"ntp source {mgmt_if}")
    lines.append(f"ip default-gateway {default_gw}")

    return lines


def _format_vlan_name(name: str) -> str:
    """Return a VLAN name safe for configuration usage (no spaces)."""

    if not name:
        return name

    stripped = name.strip()
    if not stripped:
        return stripped

    return re.sub(r"\s+", "_", stripped)


def export_config_with_templates(
    rows: List[List[str]],
    out_dir: str,
    which: str,
    iface_cfgs: Dict[Tuple[str, str], List[str]],
    *,
    switch_number: Union[str, int],
    is_less_than_200: bool,
    hostname_map: Optional[Dict[str, str]] = None,
    include_vlan_623: bool = False,
    host_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    switch_suffix = str(switch_number).strip()
    if not switch_suffix:
        switch_suffix = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_map = {
        "POE":      f"{switch_suffix}_UXM_Config.txt",
        "AMBAR_T":  f"{switch_suffix}_AMBAR_Config.txt",
        "UCA":      f"{switch_suffix}_UCA_Config.txt",
    }
    txt = os.path.join(out_dir, out_map[which])

    rows_sorted = [r for r in rows if r[-1] == which and r[0] != "LIBRE"]

    rows_by_switch: Dict[str, List[List[str]]] = {}
    for row in rows_sorted:
        rows_by_switch.setdefault(row[5], []).append(row)

    for sw_rows in rows_by_switch.values():
        sw_rows.sort(key=lambda r: _stack_interface_sort_key(r[3]))

    poe_display_map: Dict[str, str] = {}
    if which == "POE":
        meta_by_name = {
            str(info.get("sw_name")): info for info in POE_MEMBER_METADATA.values()
        }
        for sw_name, sw_rows in rows_by_switch.items():
            interfaces = [row[3] for row in sw_rows]
            tags = [row[8] for row in sw_rows]
            poe_display_map[sw_name] = _poe_display_label(
                sw_name,
                interfaces=interfaces,
                tags=tags,
                meta_by_name=meta_by_name,
            )

    poe_default_gateway = "10.192.131.254" if is_less_than_200 else "10.192.130.254"
    if which in ("POE", "AMBAR_T"):
        default_gateway_override: Optional[str] = poe_default_gateway
    elif which == "UCA":
        default_gateway_override = "10.192.129.254"
    else:
        default_gateway_override = None

    used_vlans: Set[str] = set()
    for r in rows_sorted:
        vlan = r[6]
        if vlan and vlan.isdigit():
            used_vlans.add(vlan)
        m_voice = re.search(r"VOICE=(\d+)", r[8] or "")
        if m_voice:
            used_vlans.add(m_voice.group(1))
        used_vlans.update(_vlans_from_tag_string(r[8]))

    if which == "POE":
        used_vlans.add("623")
        if include_vlan_623:
            used_vlans.update(_collect_group_vlans(rows, "UCA"))

    if which == "AMBAR_T":
        used_vlans.add("623")

    if which == "UCA":
        used_vlans.add("2230")
        if include_vlan_623:
            used_vlans.add("623")

    forced_hostname = None
    if hostname_map:
        forced_hostname = hostname_map.get(which)

    if not forced_hostname:
        if which in ("POE", "AMBAR_T"):
            forced_hostname = "AMBAR-SW"
        elif which == "UCA":
            forced_hostname = "UCA-SW"
        else:
            forced_hostname = "VIDEO-SW"
    switch_locations: Dict[str, str] = {}
    switch_contacts: Dict[str, str] = {}
    if host_metadata:
        switch_locations = _collect_switch_locations(rows, which, host_metadata)
        switch_contacts = _collect_switch_contacts(rows, which, host_metadata)

    core_lines: List[str] = []

    with open(txt, "w", encoding="utf-8") as f:
        f.write(f"!\n! Configuración generada ({which})\n!\n")
        _emit_base_template(
            f,
            forced_hostname=forced_hostname,
            which=which,
            include_vlan_623=include_vlan_623,
            default_gateway_override=default_gateway_override,
        )
        f.write("! ------------------------------------------------------------\n")

        cleaned_vlans = {v for v in used_vlans if v and v.isdigit()}

        if cleaned_vlans:
            for vlan in sorted(cleaned_vlans, key=int):
                f.write(f"vlan {vlan}\n")
                vlan_name = VLAN_NAME_MAP.get(vlan)
                if vlan_name:
                    f.write(f" name {_format_vlan_name(vlan_name)}\n")
                else:
                    f.write(f" name VLAN_{vlan}\n")
                f.write("!\n")
        if which == "UCA" and include_vlan_623:
            f.write("interface Vlan2230\n")
            f.write(" shutdown\n")
            f.write("!\n")
            mgmt_lines = _build_management_vlan_lines(
                rows_sorted,
                iface_cfgs,
                host_metadata,
                default_gateway=default_gateway_override,
            )
            for line in mgmt_lines:
                f.write(line + "\n")

        if which == "UCA":
            uca_allowed_set = {v for v in cleaned_vlans if v != "623"}
            if not include_vlan_623:
                uca_allowed_set.add("2230")
            try:
                uca_allowed = sorted(uca_allowed_set, key=int)
            except ValueError:
                uca_allowed = sorted(uca_allowed_set)
            if uca_allowed:
                for uplink_if, desc in _uca_uplink_interfaces():
                    core_lines.extend(
                        [
                            f"interface {uplink_if}",
                            f" description {desc}",
                            " switchport mode trunk",
                            " channel-group 1 mode active",
                            "!",
                        ]
                    )
                core_lines.extend(
                    [
                        "interface Port-channel1",
                        " description Port-channel 1 Conexion Core",
                        f" switchport trunk allowed vlan {','.join(uca_allowed)}",
                        " switchport mode trunk",
                        "!",
                    ]
                )

        if which == "POE":
            mgmt_lines = _build_management_vlan_lines(
                rows_sorted,
                iface_cfgs,
                host_metadata,
                default_gateway=poe_default_gateway,
            )
            for line in mgmt_lines:
                f.write(line + "\n")

            poe_allowed = sorted(cleaned_vlans, key=int)
            uplink_defs = _poe_uplink_interfaces()
            if poe_allowed and uplink_defs:
                for uplink_if, desc in uplink_defs:
                    core_lines.extend(
                        [
                            f"interface {uplink_if}",
                            f" description {desc}",
                            " switchport mode trunk",
                            " channel-group 1 mode active",
                            "!",
                        ]
                    )
                core_lines.extend(
                    [
                        "interface Port-channel1",
                        " description Port-channel 1 Conexion Core",
                        f" switchport trunk allowed vlan {','.join(poe_allowed)}",
                        " switchport mode trunk",
                        "!",
                    ]
                )

        if which == "AMBAR_T":
            mgmt_lines = _build_management_vlan_lines(
                rows_sorted,
                iface_cfgs,
                host_metadata,
                default_gateway=poe_default_gateway,
            )
            for line in mgmt_lines:
                f.write(line + "\n")

            try:
                ambar_allowed = sorted(cleaned_vlans, key=int)
            except ValueError:
                ambar_allowed = sorted(cleaned_vlans)
            uplink_defs = _ambar_t_uplink_interfaces()
            if ambar_allowed and uplink_defs:
                for uplink_if, desc in uplink_defs:
                    core_lines.extend(
                        [
                            f"interface {uplink_if}",
                            f" description {desc}",
                            " switchport mode trunk",
                            " channel-group 1 mode active",
                            "!",
                        ]
                    )
                core_lines.extend(
                    [
                        "interface Port-channel1",
                        " description Port-channel 1 Conexion Core",
                        f" switchport trunk allowed vlan {','.join(ambar_allowed)}",
                        " switchport mode trunk",
                        "!",
                    ]
                )
        f.write("! ------------------------------------------------------------\n")

        if not rows_by_switch:
            f.write("!\nend\n!\n")
            return txt

        for sw_new in sorted(rows_by_switch):
            contact_line = switch_contacts.get(sw_new, DEFAULT_SNMP_CONTACT)
            f.write(f"snmp-server contact {contact_line}\n")
            location = switch_locations.get(sw_new)
            if location:
                f.write(f"snmp-server location {location}\n")
            f.write("!\n")
            display_name = poe_display_map.get(sw_new, sw_new) if which == "POE" else sw_new
            f.write(f"! Interfaces para {display_name}\n")
            for sw_act, if_act, desc_act, if_new, desc_new, _, vlan, mode, tags, mac, _ in rows_by_switch[sw_new]:
                block = iface_cfgs.get((sw_act, if_act))
                f.write(f"interface {if_new}\n")

                commands: List[str] = []
                mode_lc = (mode or "").lower()
                allowed_set = _vlans_from_tag_string(tags)
                try:
                    allowed_list = sorted(allowed_set, key=int)
                except ValueError:
                    allowed_list = sorted(allowed_set)
                allowed_str = ",".join(allowed_list) if allowed_list else ""
                if block:
                    filtered = _filter_out_sticky(block)
                    has_desc = any(re.match(r"^\s*description\b", ln, re.IGNORECASE) for ln in filtered)
                    if (not has_desc) and desc_new and desc_new != "N/A":
                        commands.append(f" description {desc_new}")
                    for ln in filtered:
                        if re.match(r"^\s*interface\b", ln, re.IGNORECASE):
                            continue
                        commands.append(ln if ln.endswith("\n") else ln)
                else:
                    commands.append(f" ! running-config no encontrado para {sw_act} {if_act}")
                    if desc_new and desc_new != "N/A":
                        commands.append(f" description {desc_new}")
                    if mode_lc == "trunk":
                        commands.append(" switchport mode trunk")
                        if allowed_str:
                            commands.append(f" switchport trunk allowed vlan {allowed_str}")
                    elif vlan and vlan.isdigit():
                        commands.append(f" switchport access vlan {vlan}")
                        commands.append(" switchport mode access")

                if mode_lc == "trunk":
                    norm_cmds = {_normalize_command(cmd) for cmd in commands if cmd and not cmd.strip().startswith("!")}
                    if "switchport mode trunk" not in norm_cmds:
                        commands.append(" switchport mode trunk")
                    if allowed_str:
                        if all(not _normalize_command(cmd).startswith("switchport trunk allowed vlan") for cmd in commands):
                            commands.append(f" switchport trunk allowed vlan {allowed_str}")

                    disallowed = {
                        "switchport port-security mac-address sticky",
                        "switchport port-security",
                        "spanning-tree portfast",
                    }
                    commands = [
                        cmd for cmd in commands
                        if _normalize_command(cmd) not in disallowed
                    ]

                _ensure_security_basics(commands, enable_port_security=(mode_lc != "trunk"))

                m_voice = re.search(r"VOICE=(\d+)", tags or "")
                if m_voice:
                    voice_cmd = f" switchport voice vlan {m_voice.group(1)}"
                    if all(_normalize_command(cmd) != _normalize_command(voice_cmd) for cmd in commands):
                        commands.append(voice_cmd)

                for cmd in commands:
                    if cmd.endswith("\n"):
                        f.write(cmd)
                    else:
                        f.write(cmd + "\n")
                f.write("!\n")

        if core_lines:
            f.write("! --- TRONCALES CORE ---\n")
            for line in core_lines:
                if line.endswith("\n"):
                    f.write(line)
                else:
                    f.write(line + "\n")
            f.write("! ------------------------------------------------------------\n")

        f.write("!\nend\n!\n")

    return txt


def export_config_video(
    rows: List[List[str]],
    out_dir: str,
    iface_cfgs: Dict[Tuple[str, str], List[str]],
    *,
    switch_number: Union[str, int],
    hostname: Optional[str] = None,
    video_level: str,
    host_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    switch_suffix = str(switch_number).strip()
    if not switch_suffix:
        switch_suffix = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    txt = os.path.join(out_dir, f"{switch_suffix}_VIDEO_Config.txt")

    rows_sorted = [r for r in rows if r[-1] == "VIDEO" and r[0] != "LIBRE"]

    rows_by_switch: Dict[str, List[List[str]]] = {}
    for row in rows_sorted:
        rows_by_switch.setdefault(row[5], []).append(row)

    display_label_map = {
        sw: _video_display_label([r[3] for r in grp]) for sw, grp in rows_by_switch.items()
    }

    metadata_map = host_metadata or {}
    base_metadata: Dict[str, Any] = {}
    if rows_sorted:
        first_host = rows_sorted[0][0]
        base_metadata = metadata_map.get(first_host, {})

    base_lines = _video_extra_base_lines(base_metadata, level=video_level)

    switch_locations = _collect_switch_locations(rows, "VIDEO", metadata_map)
    switch_contacts = _collect_switch_contacts(rows, "VIDEO", metadata_map)

    forced_hostname = hostname or "VIDEO-SW"

    with open(txt, "w", encoding="utf-8") as f:
        f.write("!\n! Configuración generada (VIDEO)\n!\n")
        f.write(f"hostname {forced_hostname}\n")
        for ln in base_lines:
            if ln.endswith("\n"):
                f.write(ln)
            else:
                f.write(ln + "\n")
        video_hosts = [row[0] for row in rows_sorted if row[0] != "LIBRE"]
        extra_sections: List[List[str]] = []
        if video_level == "3" and video_hosts:
            vlan_blocks = _collect_video_vlan_blocks(metadata_map, video_hosts)
            if vlan_blocks:
                section_lines: List[str] = ["! --- DEFINICIÓN DE VLANES NIVEL 3 ---"]
                for block in vlan_blocks:
                    for line in block:
                        section_lines.append(line)
                extra_sections.append(section_lines)

            svi_blocks = _collect_video_svi_blocks(iface_cfgs, video_hosts)
            if svi_blocks:
                section_lines = ["! --- INTERFACES VLAN NIVEL 3 ---"]
                for if_name, block in svi_blocks:
                    section_lines.append(f"interface {if_name}")
                    for ln in block:
                        section_lines.append(ln)
                    section_lines.append("!")
                extra_sections.append(section_lines)

        f.write("! ------------------------------------------------------------\n")
        for section in extra_sections:
            for line in section:
                if line.endswith("\n"):
                    f.write(line)
                else:
                    f.write(line + "\n")
            f.write("! ------------------------------------------------------------\n")

        if not rows_by_switch:
            f.write("!\nend\n!\n")
            return txt

        for sw_new in sorted(rows_by_switch):
            contact_line = DEFAULT_SNMP_CONTACT
            f.write(f"snmp-server contact {contact_line}\n")
            location = switch_locations.get(sw_new)
            if location:
                f.write(f"snmp-server location {location}\n")
            f.write("!\n")
            display_name = display_label_map.get(sw_new, sw_new)
            f.write(f"! Interfaces para {display_name}\n")
            for sw_act, if_act, desc_act, if_new, desc_new, _sw_name, _vlan, _mode, _tags, _mac, _grupo in rows_by_switch[sw_new]:
                block = iface_cfgs.get((sw_act, if_act))
                f.write(f"interface {if_new}\n")
                mode_lc = (_mode or "").lower()
                out_lines: List[str] = []

                if block:
                    filtered = _filter_out_sticky(block)
                    for ln in filtered:
                        if re.match(r"^\s*interface\b", ln, re.IGNORECASE):
                            continue
                        out_lines.append(ln if ln.endswith("\n") else ln)
                else:
                    out_lines.append(f" ! running-config no encontrado para {sw_act} {if_act}")
                    if desc_new and desc_new != "N/A":
                        out_lines.append(f" description {desc_new}")

                if mode_lc != "trunk":
                    existing = {
                        _normalize_command(cmd)
                        for cmd in out_lines
                        if cmd and not cmd.strip().startswith("!")
                    }
                    for required in (
                        " switchport port-security mac-address sticky",
                        " switchport port-security",
                    ):
                        norm = _normalize_command(required)
                        if norm and norm not in existing:
                            out_lines.append(required)
                            existing.add(norm)

                for line in out_lines:
                    if line.endswith("\n"):
                        f.write(line)
                    else:
                        f.write(line + "\n")
                f.write("!\n")

        f.write("!\nend\n!\n")

    return txt


# ---------- Helpers de entrada (logs y CLI) ----------

def sanitize_path(p: str) -> str:
    p = p.strip()
    if (p.startswith('"') and p.endswith('"')) or (p.startswith("'") and p.endswith("'")):
        p = p[1:-1]
    p = unicodedata.normalize("NFC", p)
    p = p.strip()

    # Si se pega una ruta de Windows ("X:\\...") en un entorno POSIX, tradúcela a /mnt/x/...
    if os.name != "nt":
        m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
        if m:
            drive, rest = m.groups()
            rest = rest.replace("\\", "/")
            p = f"/mnt/{drive.lower()}/{rest}"
        elif p.startswith("\\\\"):
            # Normaliza rutas UNC (\\server\share -> //server/share)
            p = "//" + p.lstrip("\\")
            p = p.replace("\\", "/")

    return p

def pick_existing_log(name: str):
    def variants(base: str):
        base_nfc = unicodedata.normalize("NFC", base)
        base_nfd = unicodedata.normalize("NFD", base)
        swap_dash = base.replace("\u2013", "-").replace("\u2014", "-")
        with_dash = base.replace("-", "\u2013")
        return list(dict.fromkeys([base, base_nfc, base_nfd, swap_dash, with_dash]))

    candidates = []
    for v in variants(name):
        candidates.extend([v,
                           os.path.join("shrunActual", v),
                           os.path.join("/mnt/data", v),
                           os.path.expanduser(v)])
    for c in candidates:
        if os.path.exists(c):
            return c

    def norm_key(s: str):
        s = unicodedata.normalize("NFC", s)
        s = s.replace("\u2013", "-").replace("\u2014", "-")
        s = " ".join(s.split())
        return s.lower()

    wanted = norm_key(name)
    search_dirs = [".", "shrunActual", "/mnt/data"]
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if norm_key(fn) == wanted:
                path = os.path.join(d, fn)
                if os.path.exists(path):
                    return path

    return None

def resolve_cli_inputs(args: Iterable[str]) -> List[str]:
    resolved = []
    missing = []
    for raw in args:
        name = sanitize_path(raw)
        picked = pick_existing_log(name)
        if picked:
            resolved.append(picked)
        else:
            missing.append(raw)
    if missing:
        for raw in missing:
            print(f"  - Error: '{raw}' no existe (probé variantes, 'shrunActual/' y '/mnt/data').")
        raise SystemExit(1)
    return resolved

def prompt_switch_number() -> int:
    while True:
        raw = input("Introduce el número del switch: ").strip()
        if not raw:
            print("  - Debes introducir un número de switch (por ejemplo 714).")
            continue
        try:
            value = int(raw)
        except ValueError:
            print("  - Introduce un número válido (por ejemplo 714).")
            continue
        return value

# ---------- Main ----------

if __name__ == "__main__":
    switch_number = prompt_switch_number()
    is_less_than_200 = switch_number < 200
    include_vlan_623 = is_less_than_200

    cli_args = sys.argv[1:]
    if cli_args:
        inputs = resolve_cli_inputs(cli_args)
    else:
        print("\nIntroduce, uno por línea, los nombres de los ficheros LOG (Enter en blanco para terminar):")
        inputs = []
        while True:
            raw = input("> ")
            if not raw.strip():
                break
            name = sanitize_path(raw)
            picked = pick_existing_log(name)
            if not picked:
                print(f"  - Aviso: '{raw}' no existe (probé variantes, 'shrunActual/' y '/mnt/data').")
                print("    Vuelve a intentarlo o deja en blanco para terminar.")
                continue
            inputs.append(picked)

    if not inputs:
        print("No se introdujeron ficheros. Saliendo.")
        raise SystemExit(0)

    print("\nIntroduce, uno por línea, los nombres de los ficheros LOG de VIDEO (Enter en blanco para terminar):")
    video_inputs: List[str] = []
    while True:
        raw = input("> ")
        if not raw.strip():
            break
        name = sanitize_path(raw)
        picked = pick_existing_log(name)
        if not picked:
            print(f"  - Aviso: '{raw}' no existe (probé variantes, 'shrunActual/' y '/mnt/data').")
            print("    Vuelve a intentarlo o deja en blanco para terminar.")
            continue
        video_inputs.append(picked)

    (
        wifi_items,
        voip_items,
        uca_items,
        ambar_other_items,
        trunk_items,
        all_iface_cfgs,
        host_metadata,
        skipped_ports,
    ) = build_inventory_from_logs(inputs)

    poe_rows, ambar_overflow = make_mapping_poe(wifi_items, voip_items, ambar_other_items, trunk_items)
    ambar_t_rows = make_mapping_ambar_t(ambar_overflow) if ambar_overflow else []
    uca_reserved_tail = UCA_RESERVED_TAIL_FREE + (1 if include_vlan_623 else 0)
    uca_rows = make_mapping_uca(uca_items, reserved_tail=uca_reserved_tail)
    _, base_uca_allowed = _collect_trunk_allowed_from_rows(uca_rows, "UCA")
    uca_trunk_allowed = base_uca_allowed
    if include_vlan_623:
        allowed_with_mgmt = _apply_uca_management_trunks(
            uca_rows,
            include_mgmt_vlan=include_vlan_623,
        )
        if allowed_with_mgmt:
            uca_trunk_allowed = allowed_with_mgmt
    _ensure_poe_downlink_trunk(poe_rows, uca_trunk_allowed)
    if video_inputs:
        video_rows, video_iface_cfgs, video_metadata = make_mapping_video(video_inputs)
        host_metadata.update(video_metadata)
    else:
        video_rows, video_iface_cfgs = [], {}

    video_level = "2"
    if video_rows:
        while True:
            raw_level = input("Nivel del switch de VIDEO (2/3): ").strip()
            if raw_level in {"2", "3"}:
                video_level = raw_level
                break
            print("  - Introduce '2' o '3' para indicar el nivel del switch de VIDEO.")

    _sort_group_rows(poe_rows)
    if ambar_t_rows:
        _sort_group_rows(ambar_t_rows)
    _sort_group_rows(uca_rows)

    wifi_switch_names = {
        str(info.get("sw_name"))
        for info in POE_MEMBER_METADATA.values()
        if info.get("type") == "wifi"
    }
    wifi_poe_rows: List[List[str]] = []
    non_wifi_poe_rows: List[List[str]] = []
    for row in poe_rows:
        if row[-1] != "POE":
            continue
        if row[5] in wifi_switch_names:
            wifi_poe_rows.append(row)
        else:
            non_wifi_poe_rows.append(row)

    all_rows = wifi_poe_rows + non_wifi_poe_rows + ambar_t_rows + uca_rows + video_rows

    out_dir = r"X:\\AENA\\Postventa\\2024\\OP046517 Renovacion Acceso AO Barcelona\\3 Documentación\\33 TIC\\Migraciones\\SalidaScript"
    os.makedirs(out_dir, exist_ok=True)

    xlsx_path = export_excel(all_rows, out_dir, switch_number)
    removed_ports_path = export_removed_ports_report(skipped_ports, out_dir, switch_number)

    hostname_plan = build_hostname_plan(
        host_metadata,
        switch_number,
        include_poe=bool(poe_rows),
        include_ambar_t=bool(ambar_t_rows),
        include_uca=bool(uca_rows),
        include_video=bool(video_rows),
    )

    cfg_poe   = export_config_with_templates(
        all_rows, out_dir, which="POE", iface_cfgs=all_iface_cfgs,
        switch_number=switch_number,
        is_less_than_200=is_less_than_200,
        hostname_map=hostname_plan,
        include_vlan_623=include_vlan_623,
        host_metadata=host_metadata,
    )
    cfg_amb_t = None
    if ambar_t_rows:
        cfg_amb_t = export_config_with_templates(
            all_rows, out_dir, which="AMBAR_T", iface_cfgs=all_iface_cfgs,
            switch_number=switch_number,
            is_less_than_200=is_less_than_200,
            hostname_map=hostname_plan,
            include_vlan_623=include_vlan_623,
            host_metadata=host_metadata,
        )
    cfg_uca_t = None
    if uca_rows:
        cfg_uca_t = export_config_with_templates(
            all_rows, out_dir, which="UCA", iface_cfgs=all_iface_cfgs,
            switch_number=switch_number,
            is_less_than_200=is_less_than_200,
            hostname_map=hostname_plan,
            include_vlan_623=include_vlan_623,
            host_metadata=host_metadata,
        )
    combined_cfgs = {**all_iface_cfgs, **video_iface_cfgs}
    cfg_video = None
    if video_rows:
        cfg_video = export_config_video(
            all_rows, out_dir,
            iface_cfgs=combined_cfgs,
            switch_number=switch_number,
            hostname=hostname_plan.get("VIDEO"),
            video_level=video_level,
            host_metadata=host_metadata,
        )

    print("\n¡Hecho!")
    if hostname_plan:
        print("  Hostnames generados:")
        name_labels = {
            "POE": "POE/UXM",
            "AMBAR_T": "AMBAR-T",
            "UCA": "UCA",
            "VIDEO": "VIDEO",
        }
        for key in ("POE", "AMBAR_T", "UCA", "VIDEO"):
            if key in hostname_plan:
                label = name_labels.get(key, key)
                print(f"    {label}: {hostname_plan[key]}")
    print(f"  Excel: {os.path.abspath(xlsx_path)}")
    print(f"  Puertos eliminados: {os.path.abspath(removed_ports_path)}")
    print(f"  Config POE:     {os.path.abspath(cfg_poe)}")
    if cfg_amb_t:
        print(f"  Config AMBAR-T: {os.path.abspath(cfg_amb_t)}")
    if cfg_uca_t:
        print(f"  Config UCA-T:   {os.path.abspath(cfg_uca_t)}")
    if cfg_video:
        print(f"  Config VIDEO:   {os.path.abspath(cfg_video)}")
