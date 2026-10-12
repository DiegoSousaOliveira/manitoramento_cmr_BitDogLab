/*
 * oled.h — display SSD1306 128x64 da BitDogLab (I2C1, GPIO 14/15).
 *
 * Desenha-se numa "tela de rascunho" (1 KB na RAM) e oled_atualizar() envia ao display
 * só se algo mudou (enviar 1 KB pelo I2C leva ~25 ms).
 */
#ifndef OLED_H
#define OLED_H

#include <stdbool.h>

#define OLED_LARGURA 128
#define OLED_ALTURA  64

bool oled_iniciar(void);   /* false = o display não respondeu */
void oled_limpar(void);
void oled_pixel(int x, int y, bool aceso);
void oled_retangulo(int x, int y, int largura, int altura, bool aceso);
void oled_inverter(int x, int y, int largura, int altura);
/* Largura em pixels de um texto (escala 1: 6 px por letra; escala 2: 12 px). */
int oled_largura_texto(const char *texto, int escala);
void oled_texto(int x, int y, const char *texto, int escala);
/* Centralizado; se não couber na escala pedida, usa a escala 1. Devolve a escala usada. */
int oled_texto_centro(int y, const char *texto, int escala);
void oled_atualizar(void);

#endif
