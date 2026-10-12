/*
 * config.h — pinos da BitDogLab, portas e tempos do protocolo.
 *
 * Os TEMPOS seguem docs/protocolo.md (E1–E12) e são os mesmos do simulador
 * (pc/tools/simulador_bitdoglab.py), que é a referência do comportamento da placa.
 * O nome e a senha do Wi-Fi NÃO ficam aqui: ficam em wifi_config.h (fora do Git).
 */
#ifndef CONFIG_H
#define CONFIG_H

/* ------------------------------------------------------------ pinos (BitDogLab) */
#define PINO_BOTAO_A        5    /* silenciar alerta (ativo em 0, pull-up) */
#define PINO_BOTAO_B        6    /* trocar modo NORMAL/NOITE */
#define PINO_BOTAO_J        22   /* botão do joystick (apertar a alavanca): próxima câmera */
#define PINO_JOY_X          27   /* eixo horizontal (ADC1) */
#define PINO_JOY_Y          26   /* eixo vertical (ADC0) */
#define ADC_JOY_X           1
#define ADC_JOY_Y           0
#define JOY_INVERTER_X      0    /* 1 = inverte o eixo (direita precisa dar x > 0) */
#define JOY_INVERTER_Y      0    /* 1 = inverte o eixo (para cima precisa dar y > 0) */
#define PINO_BUZZER         10   /* buzzer por PWM (o mesmo do projeto buzzer_BitDogLab) */
#define PINO_MATRIZ         7    /* matriz 5x5 de LEDs WS2812 */
#define PINO_OLED_SDA       14   /* display SSD1306 128x64 no I2C1 */
#define PINO_OLED_SCL       15
#define OLED_ENDERECO       0x3C

/* ------------------------------------------------------------ rede */
#define PORTA_PLACA         5006 /* a placa escuta aqui (protocolo, seção 1) */
#define NOME_NA_REDE        "BitDogLab-Sentinela"

/* ------------------------------------------------------------ protocolo (docs/protocolo.md) */
#define TAMANHO_MAXIMO      64   /* bytes por mensagem, com o \n */
#define ZONA_MORTA          12   /* E6: |eixo| < 0,12 vale 0.00 (em centésimos) */
#define REPETIR_JOY_MS      1000 /* E5: joystick fora do centro é repetido a cada 1 s */
#define MIN_INTERVALO_JOY_MS 50  /* no máximo 20 JOY por segundo */
#define REPETIR_OLA_MS      5000 /* E8: OLA a cada 5 s até o PC responder */
#define EXPIRAR_ALERTA_MS   6000 /* E2: sem reenvio por 6 s, o alerta sai da tela */
#define SEM_PC_MS           6000 /* E12: sem PING por 6 s -> "AGUARDANDO PC" com o IP */
#define INTERVALO_DUPLO_MS  400  /* E14: dois toques no A dentro disto = "clique duplo" */
#define REPETIR_ALTO_MS     1500 /* E13: buzzer contínuo repete os 3 bipes neste ritmo */
#define REPETIR_MEDIO_MS    2500 /* E13: ...e o bipe longo neste */

/* ------------------------------------------------------------ aparência e ritmo */
#define BRILHO_MATRIZ       24   /* 0–255: os WS2812 são MUITO fortes; 24 já é bem visível */
#define FREQUENCIA_BUZZER   2700 /* Hz: perto da ressonância do buzzer (som mais alto) */
#define JOY_PASSO           5    /* o joystick anda em passos de 0,05 (menos mensagens por ruído) */
#define INTERVALO_TELA_MS   100  /* OLED redesenhado até 10x/s (só envia se mudou) */
#define INTERVALO_ENTRADAS_MS 20 /* botões e joystick lidos 50x/s */
#define TEMPO_CAO_DE_GUARDA_MS 8000 /* travou por 8 s -> a placa reinicia sozinha */

#endif
