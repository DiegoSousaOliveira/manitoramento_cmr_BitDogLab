/*
 * entradas.c — botões com filtro de repique e joystick analógico (ADC de 12 bits).
 *
 * Joystick: o centro é medido ao ligar (cada joystick para num valor um pouco diferente de 2048).
 * A posição é suavizada e anda em passos de 0,05 (JOY_PASSO) com uma pequena folga: sem isso o
 * ruído do ADC mudaria o valor o tempo todo e a placa mandaria JOY sem parar.
 */
#include "entradas.h"

#include <stdlib.h>

#include "config.h"
#include "hardware/adc.h"
#include "pico/stdlib.h"

typedef struct {
    uint pino;
    bool estavel;   /* true = apertado (o pino lê 0) */
    int iguais;     /* leituras seguidas diferentes do estado estável */
} botao_t;

static botao_t botoes[3] = {{PINO_BOTAO_A, false, 0}, {PINO_BOTAO_B, false, 0}, {PINO_BOTAO_J, false, 0}};
static int centro_x = 2048, centro_y = 2048;
static float filtro_x, filtro_y;
static int passo_x = 0, passo_y = 0;
static bool a_pendente = false;  /* 1º toque no A esperando para ver se vem o 2º */
static uint32_t a_quando = 0;

static int ler_adc(uint canal) {
    adc_select_input(canal);
    int soma = 0;
    for (int i = 0; i < 4; i++) {
        soma += adc_read();
    }
    return soma / 4;
}

static int medir_centro(uint canal) {
    int soma = 0;
    for (int i = 0; i < 32; i++) {
        soma += ler_adc(canal);
        sleep_us(200);
    }
    int centro = soma / 32;
    return (centro < 1500 || centro > 2600) ? 2048 : centro; /* alguém segurando ao ligar: usa o meio */
}

void entradas_iniciar(void) {
    for (int i = 0; i < 3; i++) {
        gpio_init(botoes[i].pino);
        gpio_set_dir(botoes[i].pino, GPIO_IN);
        gpio_pull_up(botoes[i].pino);
    }
    adc_init();
    adc_gpio_init(PINO_JOY_X);
    adc_gpio_init(PINO_JOY_Y);
    centro_x = medir_centro(ADC_JOY_X);
    centro_y = medir_centro(ADC_JOY_Y);
    filtro_x = (float)centro_x;
    filtro_y = (float)centro_y;
}

/* Botão apertado de verdade = 2 leituras seguidas iguais (40 ms) — ignora o "repique" do contato. */
static bool apertou(botao_t *b) {
    bool agora = !gpio_get(b->pino);
    if (agora == b->estavel) {
        b->iguais = 0;
        return false;
    }
    if (++b->iguais < 2) {
        return false;
    }
    b->estavel = agora;
    b->iguais = 0;
    return agora;
}

/* Leitura -> centésimos, com 10% de folga nas pontas (nem todo joystick chega a 0 ou 4095). */
static int normalizar(float bruto, int centro, bool inverter) {
    float lado = bruto >= centro ? (4095.0f - centro) : (float)centro;
    float v = (bruto - centro) * 100.0f / (lado * 0.9f);
    if (v > 100) v = 100;
    if (v < -100) v = -100;
    return inverter ? -(int)v : (int)v;
}

/* Arredonda para o passo, mas só sai do passo atual com folga (evita "tremer" entre dois). */
static int degrau(int valor, int *atual) {
    if (abs(valor - *atual) > JOY_PASSO * 6 / 10) {
        *atual = ((valor + (valor >= 0 ? JOY_PASSO / 2 : -JOY_PASSO / 2)) / JOY_PASSO) * JOY_PASSO;
    }
    return *atual;
}

void entradas_ler(entradas_t *e, uint32_t agora) {
    /* Botão A: 1 toque ou 2 toques rápidos? O 1º toque só "vale" depois de INTERVALO_DUPLO_MS sem o 2º. */
    e->a = e->a_duplo = false;
    if (apertou(&botoes[0])) {
        if (a_pendente && agora - a_quando <= INTERVALO_DUPLO_MS) {
            e->a_duplo = true;
            a_pendente = false;
        } else {
            a_pendente = true;
            a_quando = agora;
        }
    } else if (a_pendente && agora - a_quando > INTERVALO_DUPLO_MS) {
        e->a = true;
        a_pendente = false;
    }
    e->b = apertou(&botoes[1]);
    e->j = apertou(&botoes[2]);
    e->bruto_x = ler_adc(ADC_JOY_X);
    e->bruto_y = ler_adc(ADC_JOY_Y);
    filtro_x += 0.5f * ((float)e->bruto_x - filtro_x);
    filtro_y += 0.5f * ((float)e->bruto_y - filtro_y);
    e->x = degrau(normalizar(filtro_x, centro_x, JOY_INVERTER_X), &passo_x);
    e->y = degrau(normalizar(filtro_y, centro_y, JOY_INVERTER_Y), &passo_y);
}
