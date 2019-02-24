from collections import OrderedDict
import numpy as np
from typing import List, Dict, Tuple

import torch
import torch.nn as nn

from utils.ast import AbstractSyntaxTree, SyntaxNode, TerminalNode
from model.gnn import GatedGraphNeuralNetwork, main, AdjacencyList
from utils.grammar import Grammar
from utils.vocab import Vocab


class Encoder(nn.Module):
    pass


class GraphASTEncoder(Encoder):
    """An encoder based on gated recurrent graph neural networks"""
    def __init__(self,
                 ast_node_encoding_size: int,
                 grammar: Grammar,
                 vocab: Vocab):
        super(GraphASTEncoder, self).__init__()

        self.gnn = GatedGraphNeuralNetwork(hidden_size=ast_node_encoding_size,
                                           layer_timesteps=[5, 5],
                                           residual_connections=dict(),
                                           num_edge_types=2)

        self.src_node_embedding = nn.Embedding(len(vocab.source) + len(grammar.syntax_types), ast_node_encoding_size)
        self.vocab = vocab
        self.grammar = grammar

    @property
    def device(self):
        return self.src_node_embedding.weight.device

    def forward(self, asts: List[AbstractSyntaxTree]):
        adj_lists, tree_node2batch_graph_node, batch_graph_node2tree_node = self.get_batched_adjacency_lists(asts)

        tree_node_init_encoding = self.get_batched_tree_init_encoding(asts, tree_node2batch_graph_node, batch_graph_node2tree_node)

        # (batch_size, node_encoding_size)
        tree_node_encoding = self.gnn.compute_node_representations(initial_node_representation=tree_node_init_encoding,
                                                                   adjacency_lists=adj_lists)

        unpacked_tree_node_encoding = self.unpack_batch_encoding(tree_node_encoding, asts, tree_node2batch_graph_node)

        return unpacked_tree_node_encoding

    def get_batched_adjacency_lists(self, asts: List[AbstractSyntaxTree]):
        tree_node2batch_graph_node = OrderedDict()

        ast_adj_list = []
        reversed_ast_adj_list = []
        terminal_nodes_adj_list = []
        reversed_terminal_nodes_adj_list = []

        for ast_id, syntax_tree in enumerate(asts):
            for prev_node, succ_node in syntax_tree.adjacency_list:
                prev_node_id = prev_node.node_id
                succ_node_id = succ_node.node_id

                # an edge from preceding to succeeding node
                node_s_batch_id = tree_node2batch_graph_node.setdefault((ast_id, prev_node_id), len(tree_node2batch_graph_node))
                node_t_batch_id = tree_node2batch_graph_node.setdefault((ast_id, succ_node_id), len(tree_node2batch_graph_node))

                ast_adj_list.append((node_s_batch_id, node_t_batch_id))
                reversed_ast_adj_list.append((node_t_batch_id, node_s_batch_id))

            # add bi-directional connection between adjacent terminal nodes
            for node_id, succ_node_id in syntax_tree.adjacent_terminal_nodes:
                cur_token_batch_id = tree_node2batch_graph_node[(ast_id, node_id)]
                next_token_batch_id = tree_node2batch_graph_node[(ast_id, succ_node_id)]

                terminal_nodes_adj_list.append((cur_token_batch_id, next_token_batch_id))
                reversed_terminal_nodes_adj_list.append((next_token_batch_id, cur_token_batch_id))

        all_nodes_num = len(tree_node2batch_graph_node)
        adj_lists = [AdjacencyList(node_num=all_nodes_num, adj_list=ast_adj_list, device=self.device),
                     AdjacencyList(node_num=all_nodes_num, adj_list=reversed_ast_adj_list, device=self.device)]

        if terminal_nodes_adj_list:
            adj_lists.extend([
                AdjacencyList(node_num=all_nodes_num, adj_list=terminal_nodes_adj_list),
                AdjacencyList(node_num=all_nodes_num, adj_list=reversed_terminal_nodes_adj_list)
            ])

        batch_graph_node2tree_node = OrderedDict([(v, k) for k, v in tree_node2batch_graph_node.items()])

        return adj_lists, tree_node2batch_graph_node, batch_graph_node2tree_node

    def get_batched_tree_init_encoding(self, asts: List[AbstractSyntaxTree],
                                       tree_node2batch_graph_node: Dict[Tuple[int, int], int],
                                       batch_graph_node2tree_node: Dict[int, Tuple[int, int]]) -> torch.Tensor:
        indices = torch.zeros(len(batch_graph_node2tree_node), dtype=torch.long, device=self.device)

        for i, (batch_node_id, (tree_id, tree_node_id)) in enumerate(batch_graph_node2tree_node.items()):
            node = asts[tree_id].id_to_node[tree_node_id]

            if node.is_variable_node:
                idx = self.vocab.source[node.var_id]
            else:
                idx = self.grammar.syntax_type_to_id[node.node_type] + len(self.vocab.source)

            indices[i] = idx

        tree_node_embedding = self.src_node_embedding(indices)

        return tree_node_embedding

    def unpack_batch_encoding(self, flattened_node_encodings: torch.Tensor,
                              batch_syntax_trees: List[AbstractSyntaxTree],
                              example_node2batch_node_map: Dict):
        batch_size = len(batch_syntax_trees)
        max_node_num = max(tree.size for tree in batch_syntax_trees)

        index = np.zeros((batch_size, max_node_num), dtype=np.int64)
        batch_tree_node_masks = torch.zeros(batch_size, max_node_num)
        for e_id, syntax_tree in enumerate(batch_syntax_trees):
            example_nodes_with_batch_id = [(example_node_id, batch_node_id)
                                           for (_e_id, example_node_id), batch_node_id
                                           in example_node2batch_node_map.items()
                                           if _e_id == e_id]
            # example_nodes_batch_id = list(map(lambda x: x[1], sorted(example_nodes_with_batch_id, key=lambda t: t[0])))
            sorted_example_nodes_with_batch_id = sorted(example_nodes_with_batch_id, key=lambda t: t[0])
            example_nodes_batch_id = [t[1] for t in sorted_example_nodes_with_batch_id]

            index[e_id, :len(example_nodes_batch_id)] = example_nodes_batch_id
            batch_tree_node_masks[e_id, :len(example_nodes_batch_id)] = 1.

        # (batch_size, max_node_num, node_encoding_size)
        batch_node_encoding = flattened_node_encodings[torch.from_numpy(index).to(flattened_node_encodings.device)]
        batch_node_encoding.data.masked_fill_((1. - batch_tree_node_masks).byte().unsqueeze(-1), 0.)

        return dict(batch_tree_node_encoding=batch_node_encoding,
                    batch_tree_node_masks=batch_tree_node_masks)
