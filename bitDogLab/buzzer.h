/*
 * buzzer.h — buzzer por PWM, com os padrões do protocolo:
 * ALTO = 3 bipes rápidos, MEDIO = 1 bipe longo (BAIXO não toca).
 * Não usa sleep: buzzer_atualizar() é chamado no laço principal e liga/desliga na hora certa.
 */
#ifndef BUZZER_H
#define BUZZER_H

#include <stdbool.h>
#include <stdint.h>

#include "placa.h"

void buzzer_iniciar(void);
void buzzer_tocar(toque_t toque, uint32_t agora);
void buzzer_parar(void);
/* "permitido" = não está no mudo, nem calado, nem no modo NOITE (se mudar no meio, cala na hora).
 * "continuo" = padrão a repetir enquanto o alerta existir (E13), ou TOQUE_NENHUM. */
void buzzer_atualizar(uint32_t agora, bool permitido, toque_t continuo);
bool buzzer_tocando(void);

#endif
