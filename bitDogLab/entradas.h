/*
 * entradas.h — botões A, B, J (joystick apertado) e a posição do joystick.
 */
#ifndef ENTRADAS_H
#define ENTRADAS_H

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    bool a, b, j;   /* true só no instante em que o botão foi APERTADO (não enquanto segura) */
    bool a_duplo;   /* E14: dois toques no A em até INTERVALO_DUPLO_MS (o "a" sozinho espera esse tempo) */
    int x, y;       /* joystick em centésimos (-100..100): x > 0 = direita, y > 0 = para cima */
    int bruto_x, bruto_y; /* leitura do ADC (0..4095), para conferir no terminal */
} entradas_t;

/* Mede o centro do joystick: NÃO mexa nele enquanto a placa liga. */
void entradas_iniciar(void);
/* Chamar a cada ~20 ms (o filtro de repique dos botões conta com esse ritmo). */
void entradas_ler(entradas_t *e, uint32_t agora);

#endif
