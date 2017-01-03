# -*- encoding: utf-8 -*-
import time
import numpy as np
import utils.logger
import checkpoint_utils
import tensorflow as tf

from actor_learner import ActorLearner, ONE_LIFE_GAMES
from algorithms.policy_based_actor_learner import BaseA3CLearner
from networks.policy_v_network import PolicyVNetwork, SequencePolicyVNetwork


logger = utils.logger.getLogger('action_sequence_actor_learner')


class ActionSequenceA3CLearner(BaseA3CLearner):
    def __init__(self, args):

        super(ActionSequenceA3CLearner, self).__init__(args)
        
        # Shared mem vars
        self.learning_vars = args.learning_vars
        self.q_target_update_steps = args.q_target_update_steps

        conf_learning = {'name': 'local_learning_{}'.format(self.actor_id),
                         'num_act': self.num_actions,
                         'args': args}
        
        self.local_network = SequencePolicyVNetwork(conf_learning)
        self.reset_hidden_state()
            
        if self.actor_id == 0:
            var_list = self.local_network.params
            self.saver = tf.train.Saver(var_list=var_list, max_to_keep=3, 
                                        keep_checkpoint_every_n_hours=2)


    def sample_action_sequence(self, state):
        value = self.session.run(
            self.local_network.output_layer_v,
            feed_dict={
                self.local_network.input_ph: [state],
            }
        )[0, 0]

        modify_state = False
        cell_state = np.zeros((1, 256*2))
        selected_action = np.hstack([np.zeros(self.num_actions), 1]) #`GO` token
        allowed_actions = np.hstack([np.ones(self.num_actions), 0])

        actions = list()

        while True:
            action_probs, cell_state = self.session.run(
                [
                    self.local_network.action_probs,
                    self.local_network.decoder_state,
                ],
                feed_dict={
                    self.local_network.modify_state:          modify_state,
                    self.local_network.input_ph:              [state],
                    self.local_network.decoder_initial_state: cell_state,
                    self.local_network.decoder_seq_lengths:   [1],
                    self.local_network.action_inputs:         [
                        [selected_action]*self.local_network.max_decoder_steps
                    ],
                    self.local_network.allowed_actions:       [
                        [allowed_actions]*self.local_network.max_decoder_steps
                    ],
                }
            )

            allowed_actions[-1] = 1 #allow decoder to select terminal state now
            selected_action = np.random.multinomial(1, action_probs[0, 0]-np.finfo(np.float32).epsneg)
            # print np.argmax(selected_action), action_probs[0, 0, np.argmax(selected_action)]
            actions.append(selected_action)
            modify_state = True

            if selected_action[self.num_actions] or len(actions) == self.local_network.max_decoder_steps:
                return actions, value


    def _run(self):
        if not self.is_train:
            return self.test()

        """ Main actor learner loop for advantage actor critic learning. """
        logger.debug("Actor {} resuming at Step {}".format(self.actor_id, 
            self.global_step.value()))

        s = self.emulator.get_initial_state()
        total_episode_reward = 0

        s_batch = []
        a_batch = []
        y_batch = []
        adv_batch = []
        
        reset_game = False
        episode_over = False
        start_time = time.time()
        steps_at_last_reward = self.local_step
        
        while (self.global_step.value() < self.max_global_steps):
            # Sync local learning net with shared mem
            self.sync_net_with_shared_memory(self.local_network, self.learning_vars)
            self.save_vars()

            local_step_start = self.local_step 
            
            rewards = []
            states = []
            actions = []
            values = []
            
            while not (episode_over 
                or (self.local_step - local_step_start 
                    == self.max_local_steps)):
                
                # Choose next action and execute it
                action_sequence, readout_v_t = self.sample_action_sequence(s)
                # if (self.actor_id == 0) and (self.local_step % 100 == 0):
                #     logger.debug("pi={}, V={}".format(readout_pi_t, readout_v_t))
                
                acc_reward = 0.0
                for a in action_sequence[:-1]:
                    new_s, reward, episode_over = self.emulator.next(a)
                    acc_reward += reward

                    if episode_over:
                        break

                reward = acc_reward
                if reward != 0.0:
                    steps_at_last_reward = self.local_step


                total_episode_reward += reward
                # Rescale or clip immediate reward
                reward = self.rescale_reward(reward)
                
                rewards.append(reward)
                states.append(s)
                actions.append(action_sequence)
                values.append(readout_v_t)
                
                s = new_s
                self.local_step += 1
                self.global_step.increment()
                
            
            # Calculate the value offered by critic in the new state.
            if episode_over:
                R = 0
            else:
                R = self.session.run(
                    self.local_network.output_layer_v,
                    feed_dict={self.local_network.input_ph:[new_s]})[0][0]
                            
             
            sel_actions = []
            for i in reversed(xrange(len(states))):
                R = rewards[i] + self.gamma * R

                y_batch.append(R)
                a_batch.append(actions[i])
                s_batch.append(states[i])
                adv_batch.append(R - values[i])
                
                sel_actions.append(np.argmax(actions[i]))
                


            seq_lengths = [len(seq) for seq in actions]
            padded_output_sequences = np.array([
                seq + [[0]*(self.num_actions+1)]*(self.local_network.max_decoder_steps-len(seq))
                for seq in a_batch
            ])

            go_input = np.zeros((len(s_batch), 1, self.num_actions+1))
            go_input[:,:,self.num_actions] = 1
            padded_input_sequences = np.hstack([go_input, padded_output_sequences[:,:-1,:]])

            print 'Sequence lengths:', seq_lengths
            print 'Actions:', [np.argmax(a) for a in a_batch[0]]

            allowed_actions = np.ones((len(s_batch), self.local_network.max_decoder_steps, self.num_actions+1))
            allowed_actions[:, 0, -1] = 0 #empty sequence is not a valid action


            feed_dict={
                self.local_network.input_ph:              s_batch, 
                self.local_network.critic_target_ph:      y_batch,
                self.local_network.adv_actor_ph:          adv_batch,
                self.local_network.decoder_initial_state: np.zeros((len(s_batch), 256*2)),
                self.local_network.action_inputs:         padded_input_sequences,
                self.local_network.action_outputs:        padded_output_sequences,
                self.local_network.allowed_actions:       allowed_actions,
                self.local_network.modify_state:          False,
                self.local_network.decoder_seq_lengths:   seq_lengths,
            }
            entropy, advantage, grads = self.session.run(
                [
                    self.local_network.entropy,
                    self.local_network.actor_advantage_term,
                    self.local_network.get_gradients
                ],
                feed_dict=feed_dict)

            print 'Entropy:', entropy, 'Adv:', advantage

            self.apply_gradients_to_shared_memory_vars(grads)     
            
            s_batch = []
            a_batch = []
            y_batch = []          
            adv_batch = []
            
            # prevent the agent from getting stuck
            if (self.local_step - steps_at_last_reward > 5000
                or (self.emulator.env.ale.lives() == 0
                    and self.emulator.game not in ONE_LIFE_GAMES)):

                steps_at_last_reward = self.local_step
                episode_over = True
                reset_game = True


            # Start a new game on reaching terminal state
            if episode_over:
                elapsed_time = time.time() - start_time
                global_t = self.global_step.value()
                steps_per_sec = global_t / elapsed_time
                perf = "{:.0f}".format(steps_per_sec)
                logger.info("T{} / STEP {} / REWARD {} / {} STEPS/s, Actions {}".format(self.actor_id, global_t, total_episode_reward, perf, sel_actions))
                
                self.log_summary(total_episode_reward)

                episode_over = False
                total_episode_reward = 0
                steps_at_last_reward = self.local_step

                if reset_game or self.emulator.game in ONE_LIFE_GAMES:
                    s = self.emulator.get_initial_state()
                    reset_game = False


