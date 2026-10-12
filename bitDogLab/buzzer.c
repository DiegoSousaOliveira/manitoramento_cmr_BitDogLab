/*
 * buzzer.c — PWM no pino do buzzer (baseado no projeto buzzer_BitDogLab do Diego).
 */
#include "buzzer.h"

#include "config.h"
#include "hardware/clocks.h"
#include "hardware/pwm.h"
#include "pico/stdlib.h"

/* Cada padrão é uma lista de (início, duração) em ms, contados a partir do toque. */
typedef struct { uint16_t inicio, duracao; } bipe_t;
static const bipe_t BIPES_ALTO[] = {{0, 100}, {160, 100}, {320, 100}}; /* 3 bipes rápidos */
static const bipe_t BIPES_MEDIO[] = {{0, 800}};                        /* 1 bipe longo */

static uint fatia;
static const bipe_t *padrao = NULL;
static int n_bipes = 0;
static uint32_t comeco;
static bool ligado = false;
static bool ja_tocou = false;
static uint32_t ultimo_toque = 0; /* quando o último padrão começou (para repetir no modo contínuo) */

static void som(bool ligar) {
    if (ligar != ligado) {
        pwm_set_enabled(fatia, ligar);
        ligado = ligar;
    }
}

void buzzer_iniciar(void) {
    gpio_set_function(PINO_BUZZER, GPIO_FUNC_PWM);
    fatia = pwm_gpio_to_slice_num(PINO_BUZZER);
    const float divisor = 64.0f;
    uint32_t topo = (uint32_t)(clock_get_hz(clk_sys) / divisor / FREQUENCIA_BUZZER);
    pwm_set_clkdiv(fatia, divisor);
    pwm_set_wrap(fatia, (uint16_t)topo);
    pwm_set_chan_level(fatia, pwm_gpio_to_channel(PINO_BUZZER), (uint16_t)(topo / 2)); /* onda quadrada */
    pwm_set_enabled(fatia, false);
}

void buzzer_tocar(toque_t toque, uint32_t agora) {
    if (toque == TOQUE_ALTO) {
        padrao = BIPES_ALTO;
        n_bipes = sizeof BIPES_ALTO / sizeof BIPES_ALTO[0];
    } else if (toque == TOQUE_MEDIO) {
        padrao = BIPES_MEDIO;
        n_bipes = 1;
    } else {
        return;
    }
    comeco = ultimo_toque = agora;
    ja_tocou = true;
}

void buzzer_parar(void) {
    padrao = NULL;
    som(false);
}

void buzzer_atualizar(uint32_t agora, bool permitido, toque_t continuo) {
    if (padrao == NULL) {
        uint32_t repetir = continuo == TOQUE_ALTO ? REPETIR_ALTO_MS : REPETIR_MEDIO_MS;
        if (continuo == TOQUE_NENHUM || !permitido || (ja_tocou && agora - ultimo_toque < repetir)) {
            return;
        }
        buzzer_tocar(continuo, agora); /* E13: toca de novo até o alerta acabar */
    }
    if (!permitido) {
        buzzer_parar(); /* apertou A ou passou para o modo NOITE: cala na hora */
        return;
    }
    uint32_t t = agora - comeco;
    bool tocar = false;
    for (int i = 0; i < n_bipes; i++) {
        tocar |= t >= padrao[i].inicio && t < (uint32_t)(padrao[i].inicio + padrao[i].duracao);
    }
    som(tocar);
    const bipe_t *ultimo = &padrao[n_bipes - 1];
    if (t >= (uint32_t)(ultimo->inicio + ultimo->duracao)) {
        buzzer_parar();
    }
}

bool buzzer_tocando(void) { return padrao != NULL; }
