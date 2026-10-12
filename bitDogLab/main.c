/*
 * main.c — firmware da placa BitDogLab do Sentinela Casa.
 *
 * A placa entra no Wi-Fi, mostra no OLED o próprio IP e espera o PC (o app manda PING para esse
 * IP — decisão 4 do projeto). Depois: recebe ALERTA/LIMPAR/PASSOU/CAM/HORA, mostra no OLED e na
 * matriz de LEDs, toca o buzzer, e manda ao PC o joystick (para mover a câmera) e os botões.
 * Protocolo: docs/protocolo.md. Comportamento de referência: pc/tools/simulador_bitdoglab.py.
 *
 * Organização:
 *   placa.c    lógica (protocolo + estado) — sem hardware, testada no PC (testes/)
 *   rede.c     Wi-Fi + UDP          oled.c / telas.c   display
 *   matriz.c   LEDs 5x5             buzzer.c           som        entradas.c   botões e joystick
 *
 * Mensagens de diagnóstico saem pela USB (abra a porta serial a 115200 para ver).
 */
#include <stdio.h>
#include <string.h>

#include "buzzer.h"
#include "config.h"
#include "entradas.h"
#include "hardware/watchdog.h"
#include "matriz.h"
#include "oled.h"
#include "pico/cyw43_arch.h"
#include "pico/stdlib.h"
#include "placa.h"
#include "rede.h"
#include "telas.h"

static placa_t placa;
static entradas_t entradas;
static ip_addr_t destino;     /* E7: de onde o PC falou por último (IP e porta) */
static uint16_t porta_destino;
static bool tem_destino = false;

static uint32_t agora_ms(void) { return to_ms_since_boot(get_absolute_time()); }

/* Manda as mensagens de uma saída ao PC (sem PC conhecido ainda, não há para onde mandar). */
static void enviar(const saida_t *s) {
    for (int i = 0; i < s->n; i++) {
        bool ok = tem_destino && rede_enviar(s->msgs[i], &destino, porta_destino);
        if (strncmp(s->msgs[i], "PONG", 4) == 0) {
            continue; /* a cada 2 s: encheria o terminal */
        }
        if (strncmp(s->msgs[i], "JOY", 3) == 0) {
            printf("-> %s (adc x=%d y=%d)%s\n", s->msgs[i], entradas.bruto_x, entradas.bruto_y, ok ? "" : " [sem PC]");
        } else {
            printf("-> %s%s\n", s->msgs[i], ok ? "" : " [sem PC: nao enviada]");
        }
    }
}

static void tratar_pacotes(uint32_t agora) {
    pacote_t pacote;
    saida_t s;
    char erro[64];
    while (rede_receber(&pacote)) {
        saida_limpar(&s);
        if (!placa_receber(&placa, pacote.dados, pacote.n, agora, &s, erro, sizeof erro)) {
            printf("x mensagem invalida de %s: %s\n", ipaddr_ntoa(&pacote.origem), erro); /* E11: ignora */
            continue;
        }
        if (!tem_destino || !ip_addr_cmp(&destino, &pacote.origem) || porta_destino != pacote.porta) {
            ip_addr_copy(destino, pacote.origem); /* E7: responde para onde o PC falou */
            porta_destino = pacote.porta;
            tem_destino = true;
            printf("PC encontrado em %s:%u\n", ipaddr_ntoa(&destino), porta_destino);
            saida_t modo;
            saida_limpar(&modo);
            placa_pc_encontrado(&placa, &modo);
            enviar(&modo);
        }
        if (strncmp(pacote.dados, "PING", 4) != 0) {
            printf("<- %.*s\n", (int)strcspn(pacote.dados, "\r\n"), pacote.dados);
        }
        if (s.toque != TOQUE_NENHUM) {
            buzzer_tocar(s.toque, agora);
        }
        enviar(&s);
    }
}

static void ler_entradas(uint32_t agora) {
    saida_t s;
    entradas_ler(&entradas, agora);
    /* A: 1 toque = mudo liga/desliga; 2 toques = cala o alerta atual (E14). D = "2 toques no A". */
    const char botoes[4] = {'A', 'D', 'B', 'J'};
    const bool apertados[4] = {entradas.a, entradas.a_duplo, entradas.b, entradas.j};
    for (int i = 0; i < 4; i++) {
        if (apertados[i]) {
            saida_limpar(&s);
            placa_apertar(&placa, botoes[i], &s);
            enviar(&s);
            if (botoes[i] == 'A') {
                printf("botao A: mudo %s\n", placa.mudo ? "LIGADO" : "desligado");
            } else if (botoes[i] == 'D') {
                printf("botao A (2 toques): alerta atual calado\n");
            }
        }
    }
    saida_limpar(&s);
    placa_mover_joystick(&placa, entradas.x, entradas.y, agora, &s);
    enviar(&s);
}

int main(void) {
    stdio_init_all();
    bool tem_oled = oled_iniciar();
    telas_inicio("iniciando...", "");
    buzzer_iniciar();
    entradas_iniciar(); /* mede o centro do joystick: não mexa nele agora */
    printf("\nSentinela Casa - placa BitDogLab (display %s)\n", tem_oled ? "ok" : "NAO respondeu");
    if (watchdog_caused_reboot()) {
        printf("Aviso: a placa tinha travado e reiniciou sozinha (cao de guarda)\n");
    }
    if (!rede_iniciar()) {
        printf("Erro no Wi-Fi: %s\n", rede_motivo());
        telas_inicio("ERRO NO WI-FI", rede_motivo());
        sleep_ms(10000);
        watchdog_reboot(0, 0, 0); /* tenta tudo de novo */
    }
    if (!matriz_iniciar()) {
        printf("Aviso: matriz de LEDs indisponivel (sem PIO livre)\n");
    }
    placa_iniciar(&placa, false); /* ainda não conhece o PC: espera o PING (E8) */
    watchdog_enable(TEMPO_CAO_DE_GUARDA_MS, true);

    uint32_t ultima_entrada = 0, ultima_tela = 0, ultimo_status = 0;
    bool led = false;
    while (true) {
        uint32_t agora = agora_ms();
        watchdog_update();
        rede_atualizar(agora);
        if (rede_conectou_agora()) {
            placa_wifi_voltou(&placa); /* E8: conhece o PC? manda OLA */
        }
        tratar_pacotes(agora);
        if (agora - ultima_entrada >= INTERVALO_ENTRADAS_MS) {
            ultima_entrada = agora;
            ler_entradas(agora);
        }
        saida_t s;
        saida_limpar(&s);
        placa_tique(&placa, agora, &s);
        enviar(&s);

        buzzer_atualizar(agora, placa_som_permitido(&placa), placa_toque_continuo(&placa));
        uint32_t cores[5][5];
        placa_matriz(&placa, agora, cores);
        matriz_mostrar(cores);
        if (agora - ultima_tela >= INTERVALO_TELA_MS) {
            ultima_tela = agora;
            telas_desenhar(&placa, agora);
            bool conectado = placa_pc_conectado(&placa, agora);
            if (conectado != led) { /* LED da Pico W aceso = conversando com o PC */
                led = conectado;
                cyw43_arch_gpio_put(CYW43_WL_GPIO_LED_PIN, led);
            }
        }
        if (agora - ultimo_status >= 10000) {
            ultimo_status = agora;
            printf("status: wifi=%d ip=%s pc=%s alerta=%s modo=%s mudo=%d som=%s joy(adc x=%d y=%d)\n", rede_estado(),
                   rede_ip(), placa_pc_conectado(&placa, agora) ? "conectado" : "aguardando",
                   placa.alerta.ativo ? placa.alerta.cam : "-", placa.modo_noite ? "NOITE" : "NORMAL", placa.mudo,
                   placa.som_continuo ? "continuo" : "uma vez", entradas.bruto_x, entradas.bruto_y);
        }
        sleep_ms(1);
    }
}
