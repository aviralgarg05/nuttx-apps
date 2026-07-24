/****************************************************************************
 * apps/examples/elf/tests/multi/multi.c
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.  The
 * ASF licenses this file to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance with the
 * License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
 * WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
 * License for the specific language governing permissions and limitations
 * under the License.
 *
 ****************************************************************************/

#include <nuttx/config.h>
#include <nuttx/compiler.h>

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#define INITIAL_COOKIE 0x13579bdf
#define CHECK_COUNT    20
#define CHECK_DELAY_US 50000

static volatile int g_bss_cookie;
static volatile int g_data_cookie = INITIAL_COOKIE;

int main(int argc, FAR char *argv[])
{
  int instance;
  int i;

  if (argc != 2)
    {
      printf("MULTI FAIL: expected an instance id\n");
      return EXIT_FAILURE;
    }

  instance = atoi(argv[1]);
  if (instance <= 0 ||
      g_bss_cookie != 0 ||
      g_data_cookie != INITIAL_COOKIE)
    {
      printf("MULTI FAIL[%d]: initial state bss=%d data=%#x\n",
             instance, g_bss_cookie, g_data_cookie);
      return EXIT_FAILURE;
    }

  g_bss_cookie = instance;
  g_data_cookie = INITIAL_COOKIE ^ instance;

  printf("MULTI[%d]: text=%p bss=%p data=%p\n",
         instance, (FAR void *)(uintptr_t)main,
         (FAR void *)&g_bss_cookie, (FAR void *)&g_data_cookie);

  for (i = 0; i < CHECK_COUNT; i++)
    {
      if (g_bss_cookie != instance ||
          g_data_cookie != (INITIAL_COOKIE ^ instance))
        {
          printf("MULTI FAIL[%d]: state changed bss=%d data=%#x\n",
                 instance, g_bss_cookie, g_data_cookie);
          return EXIT_FAILURE;
        }

      usleep(CHECK_DELAY_US);
    }

  printf("MULTI PASS[%d]\n", instance);
  return EXIT_SUCCESS;
}
