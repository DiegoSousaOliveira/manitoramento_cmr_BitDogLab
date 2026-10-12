/*
 * telas.h — o que o OLED mostra (docs/protocolo.md, seção 9 e E12).
 */
#ifndef TELAS_H
#define TELAS_H

#include <stdint.h>

#include "placa.h"

void telas_inicio(const char *linha1, const char *linha2);
void telas_desenhar(const placa_t *p, uint32_t agora);

#endif
