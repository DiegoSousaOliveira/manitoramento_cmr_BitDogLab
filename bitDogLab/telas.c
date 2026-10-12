/*
 * telas.c — desenha no OLED a situação da placa:
 *
 *   Wi-Fi    : conectando / falhou (com o motivo) / falta configurar
 *   ESPERA   : "AGUARDANDO PC" + IP e porta da placa (para cadastrar no app) — E12
 *   REPOUSO  : "MONITORANDO", hora, câmera do joystick, último PASSOU e IP
 *   ALERTA   : câmera, classe em destaque e "PARADO 12s"
 *
 * Um alerta na tela tem prioridade sobre tudo (até expirar, E2).
 */
#include "telas.h"

#include <stdio.h>
#include <string.h>

#include "oled.h"
#include "rede.h"

#define LINHA(n) (13 + (n) * 10) /* linhas de texto pequeno abaixo do título */

/* Faixa acesa no alto da tela com o título em "negativo" (letras apagadas). */
static void titulo(const char *texto) {
    oled_texto_centro(2, texto, 1);
    oled_inverter(0, 0, OLED_LARGURA, 10);
}

void telas_inicio(const char *linha1, const char *linha2) {
    oled_limpar();
    titulo("SENTINELA CASA");
    oled_texto_centro(LINHA(1), linha1, 1);
    oled_texto_centro(LINHA(2), linha2, 1);
    oled_atualizar();
}

static void tela_wifi(void) {
    char texto[32];
    titulo("SENTINELA CASA");
    if (rede_estado() == WIFI_SEM_CONFIGURACAO) {
        oled_texto_centro(LINHA(0) + 2, "FALTA O WI-FI", 1);
        oled_texto_centro(LINHA(2), "Coloque o nome e a", 1);
        oled_texto_centro(LINHA(3), "senha da rede em", 1);
        oled_texto_centro(LINHA(4), "wifi_config.h", 1);
        return;
    }
    bool falhou = rede_estado() == WIFI_FALHOU;
    oled_texto_centro(LINHA(0) + 2, falhou ? "WI-FI FALHOU" : "CONECTANDO WI-FI", 1);
    snprintf(texto, sizeof texto, "%.21s", rede_nome());
    oled_texto_centro(LINHA(1) + 4, texto, 1);
    if (falhou) {
        oled_texto_centro(LINHA(2) + 6, rede_motivo(), 1);
        oled_texto_centro(LINHA(3) + 8, "tentando de novo...", 1);
    } else {
        snprintf(texto, sizeof texto, "tentativa %d", rede_tentativas());
        oled_texto_centro(LINHA(2) + 6, texto, 1);
        oled_texto_centro(LINHA(3) + 8, "(so Wi-Fi 2,4 GHz)", 1);
    }
}

static void tela_espera(void) {
    char texto[32];
    titulo("AGUARDANDO PC");
    oled_texto_centro(LINHA(0) + 1, "IP DA PLACA:", 1);
    oled_texto_centro(LINHA(1) + 1, rede_ip(), 1);
    snprintf(texto, sizeof texto, "PORTA %d", PORTA_PLACA);
    oled_texto_centro(LINHA(2) + 1, texto, 1);
    oled_texto_centro(LINHA(3) + 3, "Cadastre no app:", 1);
    oled_texto_centro(LINHA(4) + 2, "Ajustes > Placa", 1);
}

static void tela_repouso(const placa_t *p) {
    char texto[40];
    titulo(p->modo_noite ? "MONITORANDO - NOITE" : p->mudo ? "MONITORANDO - MUDO" : "MONITORANDO");
    oled_texto_centro(13, p->hora, 2);
    snprintf(texto, sizeof texto, "CAM %s", p->cam);
    oled_texto(0, 32, texto, 1);
    snprintf(texto, sizeof texto, "PASSOU %.14s", p->passou[0] ? p->passou : "-");
    oled_texto(0, 42, texto, 1);
    snprintf(texto, sizeof texto, "IP %s", rede_ip());
    oled_texto(0, 55, texto, 1);
}

static void tela_alerta(const placa_t *p, uint32_t agora) {
    char texto[40];
    snprintf(texto, sizeof texto, "ALERTA %s", p->alerta.cam);
    titulo(texto);
    oled_texto_centro(14, nome_classe(p->alerta.classe), 2);
    snprintf(texto, sizeof texto, "PARADO %lus", (unsigned long)placa_segundos_parado(p, agora));
    oled_texto_centro(34, texto, 2);
    bool sem_som = !placa_som_permitido(p);
    snprintf(texto, sizeof texto, "%s #%lu%s", nome_nivel(p->alerta.nivel), (unsigned long)p->alerta.id,
             sem_som ? "  SEM SOM" : "");
    oled_texto_centro(55, texto, 1);
}

void telas_desenhar(const placa_t *p, uint32_t agora) {
    oled_limpar();
    if (p->alerta.ativo) {
        tela_alerta(p, agora);
    } else if (rede_estado() != WIFI_CONECTADO) {
        tela_wifi();
    } else if (!placa_pc_conectado(p, agora)) {
        tela_espera();
    } else {
        tela_repouso(p);
    }
    oled_atualizar();
}
