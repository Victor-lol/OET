import copy
import json

from tqdm import tqdm

from ..baseline import SingleBehaviorRedTeamingMethod
from .common import get_init_msg, process_target_response
from .conversers import load_PAIR_models
from .system_prompts import get_attacker_system_prompt


class PAIR(SingleBehaviorRedTeamingMethod):
    use_ray = True
    
    def __init__(
        self, 
        attack_model, 
        target_model, 
        judge_model, 
        attack_max_n_tokens,
        target_max_n_tokens,
        judge_max_n_tokens, 
        cutoff_score=10, 
        max_retries=5,
        attack_temp=1.0, n_streams=5, steps=5, keep_last_n=3, **kwargs):
        """
        :param attack_model: the model to use for generating attacks (args to load_indiv_model in `conversers.py`)
        :param target_model: the model to use for generating target responses (kwargs to load_indiv_model in `conversers.py`)
        :param judge_model: the model to use for scoring attacks (args to load_judge in `conversers.py`)
        :param targets_path: the path to the targets JSON file
        :param attack_max_n_tokens: max_new_tokens for attack model.generate
        :param target_max_n_tokens: max_new_tokens for target model.generate
        :param judge_max_n_tokens: max_new_tokens for judge model.generate
        :param cutoff_score: cutoff judge score for a test case to be considered successful
        :param max_retries: number of retries for generating responses in get_attack in `conversers.py`
        :param attack_temp: the temperature used for generation in the attack model
        :param n_streams: Number of concurrent jailbreak conversations. (n_streams)
        :param steps: Number of steps to run the attack.
        :param keep_last_n: Number of responses to save in conversation history of attack model. If this is too large, then it may exceed the context window of the model.
        """

        self.cutoff_score = cutoff_score
        attackLM_args, targetLM_args, judgeLM_args = attack_model, target_model, judge_model
        self.attackLM, self.targetLM, self.judgeLM = load_PAIR_models(attackLM_args,
                                                           targetLM_args,
                                                           judgeLM_args,
                                                           attack_max_n_tokens,
                                                           target_max_n_tokens,
                                                           judge_max_n_tokens,
                                                           max_retries,
                                                           attack_temp,
                                                        )

        self.n_streams = n_streams
        self.steps = steps
        self.keep_last_n = keep_last_n


    def generate_test_cases_single_behavior(self, behavior_dict, target_sentence=None, verbose=False, **kwargs):
        """
        Generates test cases for a single behavior

        :param behavior: a dictionary specifying the behavior to generate test cases for
        :param verbose: whether to print progress
        :return: a test case and logs
        """

        # ========== Behavior meta data ==========
        after_adv = behavior_dict['after_adv']
        before_adv = behavior_dict['before_adv']

        if target_sentence:
            target = target_sentence
        elif 'target' in behavior_dict:
            target = behavior_dict['target']

        if target_sentence is not None:
            target = target_sentence

        system_prompt = get_attacker_system_prompt(before_adv, after_adv, target)
        
        print(f'Before Adv: {before_adv}\n')
        print(f'After Adv: {after_adv}\n')
        print(f'target: {target}\n')
        print('\n')

        # if verbose:
            # print(f'Behavior: {behavior_id} || Target: {target}')

        # PAIR vars
        n_streams = self.n_streams
        keep_last_n = self.keep_last_n
        steps = self.steps
        cutoff_score = self.cutoff_score

        attackLM = self.attackLM
        targetLM = self.targetLM
        judgeLM = self.judgeLM
        
        # Initialize conversations
        init_msg = get_init_msg(before_adv, target, after_adv)
        processed_response_list = [init_msg for _ in range(n_streams)]
        convs_list = [copy.deepcopy(attackLM.template) for _ in range(n_streams)]

        for conv in convs_list:
            conv.set_system_message(system_prompt)

        # Begin PAIR
        conversation_streams = {id : [] for id in range(n_streams)}
        best_adv_prompt = before_adv
        highest_score = 0
        suffixs = []
        scores = []
        pbar = tqdm(range(steps))
        for step in range(0, steps):
            if step > 0:
                processed_response_list = [process_target_response(completion, score, before_adv, target, after_adv) for completion, score in zip(completions, judge_scores)]
            # ======== Get adversarial prompts and improvement =========
            # This will also in-place add another conversation turn to convs_list
            extracted_attack_list = attackLM.get_attack(convs_list, processed_response_list)

            # extracted_attack_list, convs_list = clean_attacks_and_convs(extracted_attack_list, convs_list)
            
            try:
                adv_prompt_list = [attack["prompt"] for attack in extracted_attack_list]
                improv_list = [attack["improvement"] for attack in extracted_attack_list]
            except:
                adv_prompt_list = extracted_attack_list
                improv_list = extracted_attack_list
                    
            # ======== Get target responses ========
            completions = targetLM.get_response(adv_prompt_list)
            
            # ======== Get judge (GPT) scores ========
            judge_scores = judgeLM.score(adv_prompt_list, completions, before_adv, after_adv)

            # ======== Handling outputs and scores ========
            jailbroken = False
            for id, conv, adv_prompt, improv, completion, score in zip(conversation_streams, convs_list, adv_prompt_list, improv_list, completions, judge_scores):
                # Logging
                log = dict(adv_prompt=adv_prompt, improv=improv, completion=completion, score=score)
                conversation_streams[id].append(log)

                # Check for jailbreaks and updating current best_adv_prompt
                if score >= cutoff_score:
                    jailbroken = True
                if score > highest_score:
                    highest_score = score
                    best_adv_prompt = adv_prompt
                scores.append(score)
                suffixs.append(adv_prompt)
                # Truncate conversation to avoid context length issues
                conv.messages = conv.messages[-2*(keep_last_n):]

            pbar.update(1)
            pbar.set_description(f'Training score: {highest_score}')
            
            # ========= Done if found a score == 10 ======
            if jailbroken:
                break
        pbar.close()

        # if not before_adv:
        #     test_case = best_adv_prompt
        # else:
        #     test_case = f"{context_str}\n\n---\n\n{best_adv_prompt}"
            
        # logs = [s for s in conversation_streams.values()]

        logs = {
            'all_losses': scores,
            'suffix': suffixs,
            # 'all_test_cases': [s for s in conversation_streams.values()],
        }
        return logs
    
        # return test_case, logs

def clean_attacks_and_convs(attack_list, convs_list):
    """
    Remove any failed attacks (which appear as None) and corresponding conversations
    """

    # tmp = [(a, c) for (a, c) in zip(attack_list, convs_list) if a is not None]

    tmp = []
    for a, c in zip(attack_list, convs_list):
        if a is not None:
            tmp.append((a,c))
        else:
            tmp.append(('prompt', c))
    tmp = [*zip(*tmp)]

    if len(tmp) == 0:
        tmp += [' ', ' ']
    # attack_list, convs_list = list(tmp[0]), list(tmp[1])

    # return attack_list, convs_list
    return attack_list, convs_list