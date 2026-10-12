/*
 * matriz.h — matriz 5x5 de LEDs WS2812 da BitDogLab (GPIO 7), controlada pelo PIO.
 */
#ifndef MATRIZ_H
#define MATRIZ_H

#include <stdbool.h>
#include <stdint.h>

bool matriz_iniciar(void);
/* cores[linha][coluna] em 0xRRGGBB (0 = apagado), linha 0 = em cima, coluna 0 = à esquerda.
 * Só envia se mudou; o brilho é reduzido (BRILHO_MATRIZ em config.h). */
void matriz_mostrar(const uint32_t cores[5][5]);

#endif
