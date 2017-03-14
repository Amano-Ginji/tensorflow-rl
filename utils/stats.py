# -*- encoding: utf-8 -*-
import numpy as np


def kl_divergence(P, Q):
	eps = 1e-10
	return (P * np.log((P + eps) / (Q + eps))).sum()


def jenson_shannon_divergence(P, Q):
	M = 0.5 * (P + Q)
	return 0.5 * (kl_divergence(P, M) + kl_divergence(Q, M))


def ar1_process(x_previous, mean, theta, sigma):
	'''Discrete Ornstein–Uhlenbeck / AR(1) process to produce temporally correlated noise'''
	return theta*(mean - x_previous) + sigma*np.random.normal()